import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import httpx

from backend.scraper import AudioBookBayScraper
from backend.mam import MAMScraper
from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://qbittorrent.servarr.svc.cluster.local")
QBITTORRENT_USER = os.getenv("QBITTORRENT_USER", "admin")
QBITTORRENT_PASS = os.getenv("QBITTORRENT_PASS", "")
AUDIOBOOKSHELF_URL = os.getenv("AUDIOBOOKSHELF_URL", "http://audiobookshelf.audiobookshelf.svc.cluster.local")
AUDIOBOOKSHELF_TOKEN = os.getenv("AUDIOBOOKSHELF_TOKEN", "")
MAM_EMAIL = os.getenv("MAM_EMAIL", "")
MAM_PASSWORD = os.getenv("MAM_PASSWORD", "")


class DownloadRequest(BaseModel):
    magnet_url: str
    author: str = ""
    title: str = ""
    force: bool = False


# Global scraper instances
scraper: AudioBookBayScraper | None = None
mam_scraper: MAMScraper | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup scrapers."""
    global scraper, mam_scraper
    scraper = AudioBookBayScraper()
    if MAM_EMAIL and MAM_PASSWORD:
        mam_scraper = MAMScraper(MAM_EMAIL, MAM_PASSWORD)
        logger.info("MAM scraper initialized")
    # Start periodic sync between Audiobookshelf and qBittorrent
    sync_task = asyncio.create_task(_periodic_sync())
    yield
    sync_task.cancel()
    if scraper:
        await scraper.close()
    if mam_scraper:
        await mam_scraper.close()


app = FastAPI(
    title="Audiobook Search Service",
    description="Search AudioBookBay and download via n8n webhook",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/search", response_model=list[AudiobookResult])
async def search_audiobooks(q: str = Query(..., description="Search query")):
    """Search for audiobooks across all sources."""
    if not scraper:
        raise HTTPException(status_code=500, detail="Scraper not initialized")

    # Search all sources in parallel
    tasks = [scraper.search(q)]
    if mam_scraper:
        tasks.append(mam_scraper.search(q))

    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for r in all_results:
        if isinstance(r, list):
            results.extend(r)
        elif isinstance(r, Exception):
            logger.warning(f"Search source failed: {r}")

    # MAM results first (private tracker = better quality), then ABB
    results.sort(key=lambda x: (0 if x.source == "mam" else 1))
    return results


@app.get("/audiobook/{book_id:path}", response_model=AudiobookDetail)
async def get_audiobook_detail(book_id: str):
    """Get detailed information for a specific audiobook."""
    # Route to correct scraper based on source prefix
    if book_id.startswith("mam:"):
        if not mam_scraper:
            raise HTTPException(status_code=500, detail="MAM scraper not configured")
        torrent_id = book_id[4:]  # Strip "mam:" prefix
        detail = await mam_scraper.get_detail(torrent_id)
    else:
        if not scraper:
            raise HTTPException(status_code=500, detail="Scraper not initialized")
        detail = await scraper.get_detail(book_id)

    if not detail:
        raise HTTPException(status_code=404, detail="Audiobook not found or magnet link unavailable")

    return detail


@app.post("/download")
async def download_audiobook(request: Request):
    """Send magnet to qBittorrent, save to audiobookshelf path, trigger scan when done."""
    try:
        body = await request.json()
        req = DownloadRequest(**body)
    except Exception as e:
        raw = await request.body()
        logger.error(f"Failed to parse download request: {e}, raw body: {raw[:200]}, content-type: {request.headers.get('content-type')}")
        raise HTTPException(status_code=400, detail=f"Invalid request body: {e}")

    author = req.author.strip() or "Unknown Author"
    title = req.title.strip() or "Unknown Title"
    save_path = f"/audiobooks/{author}/{title}"

    # Check if book already exists in Audiobookshelf (unless force=True)
    if not req.force and AUDIOBOOKSHELF_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=15.0) as abs_client:
                libs_resp = await abs_client.get(
                    f"{AUDIOBOOKSHELF_URL}/api/libraries",
                    headers={"Authorization": f"Bearer {AUDIOBOOKSHELF_TOKEN}"},
                )
                for lib in libs_resp.json().get("libraries", []):
                    search_resp = await abs_client.get(
                        f"{AUDIOBOOKSHELF_URL}/api/libraries/{lib['id']}/search",
                        params={"q": title, "limit": 10},
                        headers={"Authorization": f"Bearer {AUDIOBOOKSHELF_TOKEN}"},
                    )
                    results = search_resp.json().get("book", [])
                    for item in results:
                        book_data = item.get("libraryItem", {}).get("media", {}).get("metadata", {})
                        existing_title = book_data.get("title", "").lower()
                        existing_author = book_data.get("authorName", "").lower()
                        if title.lower() in existing_title or existing_title in title.lower():
                            if not author or author == "Unknown Author" or author.lower() in existing_author or existing_author in author.lower():
                                raise HTTPException(
                                    status_code=409,
                                    detail=f"Book already exists in Audiobookshelf: '{book_data.get('title')}' by {book_data.get('authorName')}"
                                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Audiobookshelf duplicate check failed (proceeding anyway): {e}")

    is_mam_torrent = req.magnet_url.startswith("https://www.myanonamouse.net/")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Login to qBittorrent
            login_resp = await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            if login_resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"qBittorrent login failed: {login_resp.status_code}")

            if is_mam_torrent:
                # MAM: download .torrent file and upload to qBittorrent
                if not mam_scraper:
                    raise HTTPException(status_code=500, detail="MAM scraper not configured")

                # Extract torrent ID from URL
                import re as _re
                tid_match = _re.search(r"tid=(\d+)", req.magnet_url) or _re.search(r"/download\.php/(\d+)", req.magnet_url)
                if not tid_match:
                    raise HTTPException(status_code=400, detail="Invalid MAM torrent URL")

                torrent_data = await mam_scraper.download_torrent_file(tid_match.group(1))
                if not torrent_data:
                    raise HTTPException(status_code=502, detail="Failed to download .torrent file from MAM")

                # Upload .torrent file to qBittorrent
                resp = await client.post(
                    f"{QBITTORRENT_URL}/api/v2/torrents/add",
                    files={"torrents": ("audiobook.torrent", torrent_data, "application/x-bittorrent")},
                    data={
                        "savepath": save_path,
                        "category": "audiobooks",
                    },
                )
            else:
                # AudioBookBay: use magnet link
                # Delete existing torrent if present (supports re-download after deletion)
                import re as _re
                hash_match = _re.search(r"btih:([a-fA-F0-9]{40})", req.magnet_url)
                if hash_match:
                    info_hash = hash_match.group(1).lower()
                    await client.post(
                        f"{QBITTORRENT_URL}/api/v2/torrents/delete",
                        data={"hashes": info_hash, "deleteFiles": "false"},
                    )

                # Add torrent to qBittorrent (cookies from login are on the client)
                resp = await client.post(
                    f"{QBITTORRENT_URL}/api/v2/torrents/add",
                    data={
                        "urls": req.magnet_url,
                        "savepath": save_path,
                        "category": "audiobooks",
                    },
                )

            if resp.status_code != 200 or resp.text.strip().lower() != "ok.":
                raise HTTPException(status_code=502, detail=f"qBittorrent rejected torrent: {resp.text}")

        # Start background task to poll completion and trigger library scan
        asyncio.create_task(_poll_and_scan(req.magnet_url, author, title, save_path))

        return {"status": "ok", "message": f"Download started → {save_path}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to add torrent: {e}")


async def _poll_and_scan(magnet_url: str, author: str, title: str, save_path: str = ""):
    """Poll qBittorrent until download completes, then trigger Audiobookshelf scan."""
    import re

    # Try to extract info hash from magnet URL (works for ABB)
    info_hash = None
    hash_match = re.search(r"btih:([a-fA-F0-9]{40})", magnet_url)
    if hash_match:
        info_hash = hash_match.group(1).lower()

    poll_interval = 30
    max_polls = 120  # 1 hour max

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Login to qBittorrent for polling
        try:
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
        except Exception as e:
            logger.warning(f"qBittorrent login for polling failed: {e}")
            return

        for i in range(max_polls):
            await asyncio.sleep(poll_interval)
            try:
                if info_hash:
                    resp = await client.get(
                        f"{QBITTORRENT_URL}/api/v2/torrents/info",
                        params={"hashes": info_hash},
                    )
                    torrents = resp.json()
                else:
                    # For MAM torrents, find by save_path
                    resp = await client.get(
                        f"{QBITTORRENT_URL}/api/v2/torrents/info",
                        params={"category": "audiobooks"},
                    )
                    torrents = [t for t in resp.json() if t.get("save_path", "") == save_path]

                if not torrents:
                    continue

                torrent = torrents[0]
                progress = torrent.get("progress", 0)
                state = torrent.get("state", "")
                logger.info(f"[{author} - {title}] Progress: {progress:.0%}, State: {state}")

                if progress >= 1.0:
                    logger.info(f"[{author} - {title}] Download complete, triggering library scan")
                    await _trigger_audiobookshelf_scan(client)
                    return
            except Exception as e:
                logger.warning(f"Poll error: {e}")

    logger.warning(f"[{author} - {title}] Timed out waiting for download")


async def _trigger_audiobookshelf_scan(client: httpx.AsyncClient):
    """Trigger Audiobookshelf library scan."""
    if not AUDIOBOOKSHELF_TOKEN:
        logger.warning("AUDIOBOOKSHELF_TOKEN not set, skipping library scan")
        return

    try:
        # Get libraries
        resp = await client.get(
            f"{AUDIOBOOKSHELF_URL}/api/libraries",
            headers={"Authorization": f"Bearer {AUDIOBOOKSHELF_TOKEN}"},
        )
        libraries = resp.json().get("libraries", [])
        for lib in libraries:
            lib_id = lib["id"]
            logger.info(f"Scanning library: {lib['name']} ({lib_id})")
            await client.post(
                f"{AUDIOBOOKSHELF_URL}/api/libraries/{lib_id}/scan",
                headers={"Authorization": f"Bearer {AUDIOBOOKSHELF_TOKEN}"},
            )
    except Exception as e:
        logger.warning(f"Failed to trigger Audiobookshelf scan: {e}")


@app.get("/downloads")
async def list_downloads():
    """Get status of all audiobook downloads from qBittorrent."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Login
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            # Get audiobook torrents
            resp = await client.get(
                f"{QBITTORRENT_URL}/api/v2/torrents/info",
                params={"category": "audiobooks"},
            )
            torrents = resp.json()
            return [
                {
                    "hash": t["hash"],
                    "name": t["name"],
                    "progress": t["progress"],
                    "size": t["total_size"],
                    "downloaded": t["downloaded"],
                    "speed": t["dlspeed"],
                    "eta": t["eta"],
                    "state": t["state"],
                    "save_path": t["save_path"],
                }
                for t in torrents
            ]
    except Exception as e:
        return []


@app.delete("/downloads/{torrent_hash}")
async def delete_download(torrent_hash: str, delete_files: bool = False):
    """Delete a torrent from qBittorrent. If files are deleted, triggers Audiobookshelf scan."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()},
            )
            # If files were deleted, trigger Audiobookshelf scan to remove the book
            if delete_files:
                await _trigger_audiobookshelf_scan(client)
            return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to delete torrent: {e}")


async def _periodic_sync():
    """Periodically sync Audiobookshelf and qBittorrent — remove orphaned torrents/books."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            await _run_sync()
        except Exception as e:
            logger.warning(f"Sync error: {e}")


async def _run_sync():
    """Remove torrents whose files have been deleted (e.g. book removed from Audiobookshelf)
    and trigger a library scan if torrent files were cleaned up."""
    if not AUDIOBOOKSHELF_TOKEN:
        return

    changed = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Login to qBittorrent
        await client.post(
            f"{QBITTORRENT_URL}/api/v2/auth/login",
            data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
        )

        # Get all audiobook torrents
        resp = await client.get(
            f"{QBITTORRENT_URL}/api/v2/torrents/info",
            params={"category": "audiobooks"},
        )
        torrents = resp.json()

        for t in torrents:
            save_path = t.get("save_path", "")
            if not save_path or not save_path.startswith("/audiobooks/"):
                continue

            # If save_path no longer exists on disk, the book was deleted — remove torrent
            if not os.path.exists(save_path):
                logger.info(f"Sync: removing orphaned torrent '{t['name']}' (path gone: {save_path})")
                await client.post(
                    f"{QBITTORRENT_URL}/api/v2/torrents/delete",
                    data={"hashes": t["hash"], "deleteFiles": "true"},
                )
                changed = True

        # Also check: completed torrents whose files exist but aren't in Audiobookshelf
        # (reverse direction is handled by Audiobookshelf's own library scan)

    # If we cleaned up torrents, trigger a library scan
    if changed:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await _trigger_audiobookshelf_scan(client)


@app.post("/sync")
async def trigger_sync():
    """Manually trigger sync between Audiobookshelf and qBittorrent."""
    try:
        await _run_sync()
        return {"status": "ok", "message": "Sync completed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@app.get("/files/{path:path}")
async def list_files(path: str):
    """List files in a download directory."""
    # Only allow access under /audiobooks/
    base = "/audiobooks"
    full_path = os.path.normpath(os.path.join(base, path))
    if not full_path.startswith(base):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Path not found")

    if os.path.isfile(full_path):
        return FileResponse(full_path, filename=os.path.basename(full_path))

    files = []
    for entry in sorted(os.listdir(full_path)):
        entry_path = os.path.join(full_path, entry)
        rel_path = os.path.relpath(entry_path, base)
        if os.path.isfile(entry_path):
            files.append({
                "name": entry,
                "size": os.path.getsize(entry_path),
                "path": rel_path,
            })
        elif os.path.isdir(entry_path):
            files.append({
                "name": entry + "/",
                "size": 0,
                "path": rel_path,
            })
    return files


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the web UI."""
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Audiobook Search</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #1a1a1a;
            color: #e0e0e0;
            line-height: 1.6;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }

        h1 {
            text-align: center;
            margin-bottom: 30px;
            color: #ffffff;
        }

        .search-box {
            display: flex;
            gap: 10px;
            margin-bottom: 30px;
        }

        input[type="text"] {
            flex: 1;
            padding: 12px 16px;
            font-size: 16px;
            border: 1px solid #444;
            border-radius: 4px;
            background: #2a2a2a;
            color: #e0e0e0;
        }

        input[type="text"]:focus {
            outline: none;
            border-color: #5a9fd4;
        }

        button {
            padding: 12px 24px;
            font-size: 16px;
            border: none;
            border-radius: 4px;
            background: #5a9fd4;
            color: white;
            cursor: pointer;
            transition: background 0.3s;
        }

        button:hover {
            background: #4a8fc4;
        }

        button:disabled {
            background: #444;
            cursor: not-allowed;
        }

        .status {
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 4px;
            display: none;
        }

        .status.success {
            background: #2d5016;
            border: 1px solid #4a8025;
            color: #90ee90;
        }

        .status.error {
            background: #5c1a1a;
            border: 1px solid #a02020;
            color: #ffb3b3;
        }

        .status.info {
            background: #1a3a5c;
            border: 1px solid #2050a0;
            color: #b3d9ff;
        }

        .results {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 20px;
        }

        .card {
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .card:hover {
            transform: translateY(-4px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }

        .card-image {
            width: 100%;
            height: 200px;
            object-fit: cover;
            background: #1a1a1a;
        }

        .card-content {
            padding: 16px;
        }

        .card-title {
            font-size: 18px;
            font-weight: bold;
            margin-bottom: 8px;
            color: #ffffff;
        }

        .card-meta {
            font-size: 14px;
            color: #aaa;
            margin-bottom: 4px;
        }

        .card-actions {
            margin-top: 12px;
        }

        .download-btn {
            width: 100%;
            padding: 10px;
            background: #4a8fc4;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background 0.3s;
        }

        .download-btn:hover {
            background: #3a7fb4;
        }

        .download-btn:disabled {
            background: #444;
            cursor: not-allowed;
        }

        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
        }

        .modal-content {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: #2a2a2a;
            padding: 24px;
            border-radius: 8px;
            border: 1px solid #444;
            max-width: 500px;
            width: 90%;
        }

        .modal-title {
            font-size: 20px;
            margin-bottom: 16px;
            color: #ffffff;
        }

        .modal-field {
            margin-bottom: 12px;
        }

        .modal-field label {
            display: block;
            margin-bottom: 4px;
            color: #ccc;
        }

        .modal-field input {
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #444;
            border-radius: 4px;
            background: #1a1a1a;
            color: #e0e0e0;
        }

        .modal-actions {
            display: flex;
            gap: 10px;
            margin-top: 16px;
        }

        .modal-actions button {
            flex: 1;
        }

        .cancel-btn {
            background: #666;
        }

        .cancel-btn:hover {
            background: #555;
        }

        .section-title {
            font-size: 24px;
            margin: 30px 0 20px 0;
            color: #ffffff;
            border-bottom: 2px solid #444;
            padding-bottom: 10px;
        }

        .downloads-list {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .download-item {
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 8px;
            padding: 16px;
        }

        .download-name {
            font-size: 16px;
            font-weight: bold;
            color: #ffffff;
            margin-bottom: 8px;
        }

        .download-meta {
            font-size: 14px;
            color: #aaa;
            margin-bottom: 4px;
        }

        .download-state {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            margin-top: 8px;
        }

        .state-downloading {
            background: #1a3a5c;
            color: #b3d9ff;
        }

        .state-seeding {
            background: #2d5016;
            color: #90ee90;
        }

        .state-paused {
            background: #5c5c1a;
            color: #ffff90;
        }

        .state-error {
            background: #5c1a1a;
            color: #ffb3b3;
        }

        .state-stalled {
            background: #5c4a1a;
            color: #ffd9b3;
        }

        .state-queued {
            background: #3a3a3a;
            color: #ccc;
        }

        .progress-bar-container {
            width: 100%;
            height: 24px;
            background: #1a1a1a;
            border-radius: 4px;
            overflow: hidden;
            margin: 8px 0;
            border: 1px solid #444;
        }

        .progress-bar-fill {
            height: 100%;
            background: #5a9fd4;
            transition: width 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 12px;
            font-weight: bold;
        }

        .progress-bar-fill.complete {
            background: #4a8025;
        }

        .no-downloads {
            text-align: center;
            padding: 40px;
            color: #aaa;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Audiobook Search</h1>
        <div style="text-align: center; margin-bottom: 20px;">
            <a href="https://audiobookshelf.viktorbarzin.me" target="_blank" style="color: #5a9fd4; text-decoration: none; font-size: 14px;">Open Audiobookshelf Library &rarr;</a>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" placeholder="Search for audiobooks...">
            <button id="searchBtn" onclick="search()">Search</button>
        </div>

        <div id="status" class="status"></div>

        <div id="results" class="results"></div>

        <h2 class="section-title">Active Downloads</h2>
        <div id="downloads" class="downloads-list"></div>
    </div>

    <div id="downloadModal" class="modal">
        <div class="modal-content">
            <h2 class="modal-title">Download Audiobook</h2>
            <div class="modal-field">
                <label for="authorInput">Author:</label>
                <input type="text" id="authorInput">
            </div>
            <div class="modal-field">
                <label for="titleInput">Title:</label>
                <input type="text" id="titleInput">
            </div>
            <div class="modal-actions" style="flex-wrap: wrap;">
                <button class="cancel-btn" onclick="closeModal()">Cancel</button>
                <button onclick="copyMagnetLink()" style="background: #6a5acd;">Copy Magnet Link</button>
                <button onclick="confirmDownload()">Send to Library</button>
            </div>
        </div>
    </div>

    <script>
        let currentMagnetUrl = null;

        function showStatus(message, type) {
            const statusEl = document.getElementById('status');
            statusEl.textContent = message;
            statusEl.className = `status ${type}`;
            statusEl.style.display = 'block';

            setTimeout(() => {
                statusEl.style.display = 'none';
            }, 5000);
        }

        function formatBytes(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        function formatSpeed(bytesPerSecond) {
            if (bytesPerSecond === 0) return '0 B/s';
            const k = 1024;
            const sizes = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
            const i = Math.floor(Math.log(bytesPerSecond) / Math.log(k));
            return parseFloat((bytesPerSecond / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        function formatETA(seconds) {
            if (seconds === 8640000 || seconds < 0) return 'Unknown';
            if (seconds === 0) return 'Complete';

            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);

            if (hours > 0) {
                return `${hours}h ${minutes}m`;
            }
            return `${minutes}m`;
        }

        function getStateClass(state) {
            const stateMap = {
                'downloading': 'state-downloading',
                'stalledDL': 'state-stalled',
                'uploading': 'state-seeding',
                'stalledUP': 'state-seeding',
                'pausedDL': 'state-paused',
                'pausedUP': 'state-paused',
                'queuedDL': 'state-queued',
                'queuedUP': 'state-queued',
                'error': 'state-error',
                'checkingUP': 'state-queued',
                'checkingDL': 'state-queued',
            };
            return stateMap[state] || 'state-queued';
        }

        function getStateText(state) {
            const stateMap = {
                'downloading': 'Downloading',
                'stalledDL': 'Stalled',
                'uploading': 'Seeding',
                'stalledUP': 'Seeding',
                'pausedDL': 'Paused',
                'pausedUP': 'Paused',
                'queuedDL': 'Queued',
                'queuedUP': 'Queued',
                'error': 'Error',
                'checkingUP': 'Checking',
                'checkingDL': 'Checking',
            };
            return stateMap[state] || state;
        }

        async function cancelDownload(hash, deleteFiles) {
            const action = deleteFiles ? 'delete with files' : 'remove';
            if (!confirm(`Are you sure you want to ${action} this download?`)) return;

            try {
                const response = await fetch(`/downloads/${hash}?delete_files=${deleteFiles}`, {method: 'DELETE'});
                if (!response.ok) throw new Error(`Server returned ${response.status}`);
                showStatus('Download removed', 'success');
                refreshDownloads();
            } catch (error) {
                showStatus('Failed to remove download: ' + error.message, 'error');
            }
        }

        async function refreshDownloads() {
            try {
                const response = await fetch('/downloads');
                if (!response.ok) {
                    throw new Error('Failed to fetch downloads');
                }

                const downloads = await response.json();
                displayDownloads(downloads);
            } catch (error) {
                console.error('Failed to refresh downloads:', error);
            }
        }

        function displayDownloads(downloads) {
            const downloadsEl = document.getElementById('downloads');

            if (downloads.length === 0) {
                downloadsEl.innerHTML = '<div class="no-downloads">No active downloads</div>';
                return;
            }

            downloadsEl.innerHTML = downloads.map(dl => {
                const progressPercent = (dl.progress * 100).toFixed(1);
                const isComplete = dl.progress >= 1.0;
                const progressBarClass = isComplete ? 'progress-bar-fill complete' : 'progress-bar-fill';

                const div = document.createElement('div');
                div.textContent = dl.name;
                const escapedName = div.innerHTML;

                return `
                    <div class="download-item">
                        <div class="download-name">${escapedName}</div>
                        <div class="progress-bar-container">
                            <div class="${progressBarClass}" style="width: ${progressPercent}%">
                                ${progressPercent}%
                            </div>
                        </div>
                        <div class="download-meta">Size: ${formatBytes(dl.size)} (${formatBytes(dl.downloaded)} downloaded)</div>
                        <div class="download-meta">Speed: ${formatSpeed(dl.speed)} | ETA: ${formatETA(dl.eta)}</div>
                        <div class="download-meta">Path: ${dl.save_path}</div>
                        <div style="display: flex; align-items: center; gap: 10px; margin-top: 8px; flex-wrap: wrap;">
                            <span class="download-state ${getStateClass(dl.state)}">${getStateText(dl.state)}</span>
                            ${isComplete ? `<button style="padding: 4px 12px; font-size: 12px; background: #4a8025; border: none; border-radius: 4px; color: white; cursor: pointer;" onclick="browseFiles('${encodeURIComponent(dl.save_path.replace(/^\\/audiobooks\\//, ''))}')">Browse Files</button>` : ''}
                            <button class="cancel-btn" style="padding: 4px 12px; font-size: 12px;" onclick="cancelDownload('${dl.hash}', false)">Remove</button>
                            <button class="cancel-btn" style="padding: 4px 12px; font-size: 12px; background: #8b2020;" onclick="cancelDownload('${dl.hash}', true)">Delete + Files</button>
                        </div>
                        <div id="files-${dl.hash}" style="display: none; margin-top: 10px; padding: 10px; background: #1a1a1a; border-radius: 4px;"></div>
                    </div>
                `;
            }).join('');
        }

        async function search() {
            const query = document.getElementById('searchInput').value.trim();
            if (!query) {
                showStatus('Please enter a search query', 'error');
                return;
            }

            const searchBtn = document.getElementById('searchBtn');
            searchBtn.disabled = true;
            showStatus('Searching...', 'info');

            try {
                const response = await fetch(`/search?q=${encodeURIComponent(query)}`);
                if (!response.ok) {
                    throw new Error('Search failed');
                }

                const results = await response.json();
                displayResults(results);

                if (results.length === 0) {
                    showStatus('No results found', 'info');
                } else {
                    showStatus(`Found ${results.length} results`, 'success');
                }
            } catch (error) {
                showStatus('Search failed: ' + error.message, 'error');
                console.error('Search error:', error);
            } finally {
                searchBtn.disabled = false;
            }
        }

        function displayResults(results) {
            const resultsEl = document.getElementById('results');

            if (results.length === 0) {
                resultsEl.innerHTML = '';
                return;
            }

            resultsEl.innerHTML = results.map(book => {
                const div = document.createElement('div');
                div.textContent = book.title;
                const escapedTitle = div.innerHTML;
                div.textContent = book.author || '';
                const escapedAuthor = div.innerHTML;

                return `
                    <div class="card">
                        ${book.cover_url ? `<img src="${book.cover_url}" alt="${escapedTitle}" class="card-image">` : ''}
                        <div class="card-content">
                            <div class="card-title">
                                <span style="display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; margin-right: 6px; vertical-align: middle; background: ${book.source === 'mam' ? '#6a5acd' : '#4a8fc4'}; color: white;">${book.source === 'mam' ? 'MAM' : 'ABB'}</span>
                                ${escapedTitle}
                            </div>
                            ${book.author ? `<div class="card-meta">Author: ${escapedAuthor}</div>` : ''}
                            ${book.narrator ? `<div class="card-meta">Narrator: ${book.narrator}</div>` : ''}
                            ${book.format ? `<div class="card-meta">Format: ${book.format}</div>` : ''}
                            ${book.size ? `<div class="card-meta">Size: ${book.size}</div>` : ''}
                            <div class="card-actions">
                                <button class="download-btn"
                                    data-book-id="${book.id}"
                                    data-book-author="${escapedAuthor}"
                                    data-book-title="${escapedTitle}"
                                    onclick="prepareDownload(this)">
                                    Download
                                </button>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        async function prepareDownload(button) {
            const bookId = button.dataset.bookId;
            const author = button.dataset.bookAuthor;
            const title = button.dataset.bookTitle;

            showStatus('Fetching download details...', 'info');

            try {
                const response = await fetch(`/audiobook/${encodeURIComponent(bookId)}`);
                if (!response.ok) {
                    const errorText = await response.text();
                    console.error('Server response:', response.status, errorText);
                    throw new Error(`Server returned ${response.status}`);
                }

                const detail = await response.json();
                if (!detail.magnet_url) {
                    throw new Error('No magnet URL in response');
                }

                currentMagnetUrl = detail.magnet_url;

                // Pre-fill the modal
                document.getElementById('authorInput').value = detail.author || author || '';
                document.getElementById('titleInput').value = detail.title || title || '';

                // Show modal
                document.getElementById('downloadModal').style.display = 'block';
            } catch (error) {
                showStatus('Failed to get download details: ' + error.message, 'error');
                console.error('Download prep error:', error);
            }
        }

        function closeModal() {
            document.getElementById('downloadModal').style.display = 'none';
            currentMagnetUrl = null;
        }

        async function copyMagnetLink() {
            if (!currentMagnetUrl) {
                showStatus('No magnet URL available', 'error');
                return;
            }
            try {
                await navigator.clipboard.writeText(currentMagnetUrl);
                showStatus('Magnet link copied to clipboard!', 'success');
                closeModal();
            } catch (e) {
                // Fallback for non-HTTPS or denied clipboard
                prompt('Copy this magnet link:', currentMagnetUrl);
            }
        }

        async function confirmDownload(force = false) {
            const author = document.getElementById('authorInput').value.trim();
            const title = document.getElementById('titleInput').value.trim();

            if (!title) {
                showStatus('Title is required', 'error');
                return;
            }

            if (!currentMagnetUrl) {
                showStatus('No magnet URL available', 'error');
                console.error('currentMagnetUrl is null or undefined:', currentMagnetUrl);
                return;
            }

            const magnetUrl = currentMagnetUrl;
            closeModal();
            showStatus('Sending download request...', 'info');

            try {
                const requestBody = {
                    magnet_url: magnetUrl,
                    author: author,
                    title: title,
                    force: force,
                };
                console.log('Sending download request:', requestBody);

                const response = await fetch('/download', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(requestBody),
                });

                if (response.status === 409) {
                    const errorData = await response.json();
                    const detail = errorData.detail || 'Book already exists in library';
                    const statusEl = document.getElementById('status');
                    statusEl.innerHTML = `${detail} <button onclick="redownload('${btoa(magnetUrl)}', '${btoa(author)}', '${btoa(title)}')" style="margin-left: 10px; padding: 4px 12px; background: #b8860b; border: none; border-radius: 4px; color: white; cursor: pointer;">Download Anyway</button>`;
                    statusEl.className = 'status info';
                    statusEl.style.display = 'block';
                    return;
                }

                if (!response.ok) {
                    const errorText = await response.text();
                    console.error('Download failed:', response.status, errorText);
                    throw new Error(`Server returned ${response.status}`);
                }

                const result = await response.json();
                showStatus(result.message || 'Download started successfully!', 'success');
                // Trigger immediate refresh of downloads list
                refreshDownloads();
            } catch (error) {
                showStatus('Failed to start download: ' + error.message, 'error');
                console.error('Download error:', error);
            }
        }

        async function browseFiles(relativePath) {
            try {
                const response = await fetch(`/files/${relativePath}`);
                if (!response.ok) throw new Error(`Server returned ${response.status}`);
                const files = await response.json();

                // Find the files div by looking for it near the clicked button
                const allFileDivs = document.querySelectorAll('[id^="files-"]');
                // Toggle: find the one whose parent download-item contains this path
                let targetDiv = null;
                for (const div of allFileDivs) {
                    const item = div.closest('.download-item');
                    if (item && item.querySelector(`[onclick*="${CSS.escape(relativePath)}"]`)) {
                        targetDiv = div;
                        break;
                    }
                }
                if (!targetDiv) return;

                if (targetDiv.style.display !== 'none') {
                    targetDiv.style.display = 'none';
                    return;
                }

                if (files.length === 0) {
                    targetDiv.innerHTML = '<div style="color: #aaa; font-style: italic;">No files found</div>';
                } else {
                    targetDiv.innerHTML = files.map(f => {
                        const isDir = f.name.endsWith('/');
                        const icon = isDir ? '&#128193;' : '&#128196;';
                        const link = isDir
                            ? `<a href="javascript:void(0)" onclick="browseFiles('${encodeURIComponent(f.path)}')" style="color: #5a9fd4; text-decoration: none;">${f.name}</a>`
                            : `<a href="/files/${encodeURIComponent(f.path)}" download style="color: #5a9fd4; text-decoration: none;">${f.name}</a>
                               <span style="color: #888; margin-left: 8px;">(${formatBytes(f.size)})</span>`;
                        return `<div style="padding: 4px 0;">${icon} ${link}</div>`;
                    }).join('');
                }
                targetDiv.style.display = 'block';
            } catch (error) {
                showStatus('Failed to list files: ' + error.message, 'error');
            }
        }

        async function redownload(magnetB64, authorB64, titleB64) {
            const magnetUrl = atob(magnetB64);
            const author = atob(authorB64);
            const title = atob(titleB64);
            showStatus('Re-sending download (force)...', 'info');
            try {
                const response = await fetch('/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ magnet_url: magnetUrl, author, title, force: true }),
                });
                if (!response.ok) throw new Error(`Server returned ${response.status}`);
                const result = await response.json();
                showStatus(result.message || 'Download started successfully!', 'success');
                refreshDownloads();
            } catch (error) {
                showStatus('Failed to start download: ' + error.message, 'error');
            }
        }

        // Enter key to search
        document.getElementById('searchInput').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                search();
            }
        });

        // Close modal on outside click
        document.getElementById('downloadModal').addEventListener('click', (e) => {
            if (e.target.id === 'downloadModal') {
                closeModal();
            }
        });

        // Auto-refresh downloads every 10 seconds
        setInterval(refreshDownloads, 10000);

        // Initial load of downloads
        refreshDownloads();
    </script>
</body>
</html>
"""
    return html_content
