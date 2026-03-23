import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

from backend.scraper import AudioBookBayScraper
from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://qbittorrent.servarr.svc.cluster.local")
QBITTORRENT_USER = os.getenv("QBITTORRENT_USER", "admin")
QBITTORRENT_PASS = os.getenv("QBITTORRENT_PASS", "")
AUDIOBOOKSHELF_URL = os.getenv("AUDIOBOOKSHELF_URL", "http://audiobookshelf.audiobookshelf.svc.cluster.local")
AUDIOBOOKSHELF_TOKEN = os.getenv("AUDIOBOOKSHELF_TOKEN", "")


class DownloadRequest(BaseModel):
    magnet_url: str
    author: str = ""
    title: str = ""


# Global scraper instance
scraper: AudioBookBayScraper | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup scraper."""
    global scraper
    scraper = AudioBookBayScraper()
    yield
    if scraper:
        await scraper.close()


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
    """Search for audiobooks on AudioBookBay."""
    if not scraper:
        raise HTTPException(status_code=500, detail="Scraper not initialized")

    results = await scraper.search(q)
    return results


@app.get("/audiobook/{book_id:path}", response_model=AudiobookDetail)
async def get_audiobook_detail(book_id: str):
    """Get detailed information for a specific audiobook."""
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

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Login to qBittorrent
            login_resp = await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            if login_resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"qBittorrent login failed: {login_resp.status_code}")

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
        asyncio.create_task(_poll_and_scan(req.magnet_url, author, title))

        return {"status": "ok", "message": f"Download started → {save_path}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to add torrent: {e}")


async def _poll_and_scan(magnet_url: str, author: str, title: str):
    """Poll qBittorrent until download completes, then trigger Audiobookshelf scan."""
    # Extract info hash from magnet URL
    import re
    hash_match = re.search(r"btih:([a-fA-F0-9]{40})", magnet_url)
    if not hash_match:
        logger.warning("Could not extract info hash from magnet URL")
        return

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
                resp = await client.get(
                    f"{QBITTORRENT_URL}/api/v2/torrents/info",
                    params={"hashes": info_hash},
                )
                torrents = resp.json()
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
    </style>
</head>
<body>
    <div class="container">
        <h1>Audiobook Search</h1>

        <div class="search-box">
            <input type="text" id="searchInput" placeholder="Search for audiobooks...">
            <button id="searchBtn" onclick="search()">Search</button>
        </div>

        <div id="status" class="status"></div>

        <div id="results" class="results"></div>
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
            <div class="modal-actions">
                <button class="cancel-btn" onclick="closeModal()">Cancel</button>
                <button onclick="confirmDownload()">Download</button>
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
                            <div class="card-title">${escapedTitle}</div>
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

        async function confirmDownload() {
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
                };
                console.log('Sending download request:', requestBody);

                const response = await fetch('/download', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(requestBody),
                });

                if (!response.ok) {
                    const errorText = await response.text();
                    console.error('Download failed:', response.status, errorText);
                    throw new Error(`Server returned ${response.status}`);
                }

                const result = await response.json();
                showStatus(result.message || 'Download started successfully!', 'success');
            } catch (error) {
                showStatus('Failed to start download: ' + error.message, 'error');
                console.error('Download error:', error);
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
    </script>
</body>
</html>
"""
    return html_content
