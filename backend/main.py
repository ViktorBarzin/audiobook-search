import os
import asyncio
import logging
import re as _re
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import httpx

from backend.scraper import AudioBookBayScraper
from backend.mam import MAMScraper
from backend.annas import AnnasArchiveScraper
from backend.libgen import LibGenScraper
from backend.openlib import OpenLibraryScraper
from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://qbittorrent.servarr.svc.cluster.local")
QBITTORRENT_USER = os.getenv("QBITTORRENT_USER", "admin")
QBITTORRENT_PASS = os.getenv("QBITTORRENT_PASS", "")
AUDIOBOOKSHELF_URL = os.getenv("AUDIOBOOKSHELF_URL", "http://audiobookshelf.audiobookshelf.svc.cluster.local")
AUDIOBOOKSHELF_TOKEN = os.getenv("AUDIOBOOKSHELF_TOKEN", "")
MAM_EMAIL = os.getenv("MAM_EMAIL", "")
MAM_PASSWORD = os.getenv("MAM_PASSWORD", "")
CWA_INGEST_PATH = os.getenv("CWA_INGEST_PATH", "/cwa-book-ingest")


class DownloadRequest(BaseModel):
    magnet_url: str
    author: str = ""
    title: str = ""
    force: bool = False
    content_type: str = "audiobook"
    source: str = ""


# Global scraper instances
scraper: AudioBookBayScraper | None = None
mam_scraper: MAMScraper | None = None
annas_scraper: AnnasArchiveScraper | None = None
libgen_scraper: LibGenScraper | None = None
openlib_scraper: OpenLibraryScraper | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup scrapers."""
    global scraper, mam_scraper, annas_scraper, libgen_scraper, openlib_scraper
    scraper = AudioBookBayScraper()
    annas_scraper = AnnasArchiveScraper()
    libgen_scraper = LibGenScraper()
    openlib_scraper = OpenLibraryScraper()
    from backend.mam import MAM_ID
    if MAM_ID or (MAM_EMAIL and MAM_PASSWORD):
        mam_scraper = MAMScraper(MAM_EMAIL, MAM_PASSWORD)
        logger.info(f"MAM scraper initialized (mam_id={'set' if MAM_ID else 'not set'})")
    sync_task = asyncio.create_task(_periodic_sync())
    yield
    sync_task.cancel()
    if scraper:
        await scraper.close()
    if mam_scraper:
        await mam_scraper.close()
    if annas_scraper:
        await annas_scraper.close()
    if libgen_scraper:
        await libgen_scraper.close()
    if openlib_scraper:
        await openlib_scraper.close()


app = FastAPI(
    title="Book Search",
    description="Search for audiobooks and ebooks across multiple sources",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/mam-status")
async def mam_status():
    """Check MAM authentication status."""
    from backend.mam import MAM_ID, SEEDBOX_URL
    result = {"mam_id_configured": bool(MAM_ID), "authenticated": False, "ip": None}
    if not MAM_ID:
        result["instructions"] = (
            "MAM_ID env var not set. Go to MAM → Preferences → Security → "
            "create an ASN-locked session with 'allow dynamic seedbox IP' enabled. "
            "Store the mam_id value in Vault at secret/servarr key 'mam_id'."
        )
        return result
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(SEEDBOX_URL, cookies={"mam_id": MAM_ID})
            data = r.json()
            result["ip"] = data.get("ip")
            result["authenticated"] = data.get("Success", False)
            result["msg"] = data.get("msg", "")
    except Exception as e:
        result["error"] = str(e)
    return result


@app.get("/stacks-status")
async def stacks_status():
    """Check Stacks download manager status."""
    if not annas_scraper:
        return {"available": False}
    return await annas_scraper.get_stacks_status()


@app.get("/sources")
async def get_sources():
    """Get status of all configured search sources."""
    from backend.mam import MAM_ID
    sources = {
        "abb": {"name": "AudioBookBay", "type": "audiobook", "available": scraper is not None},
        "mam": {"name": "MyAnonamouse", "type": "both", "available": mam_scraper is not None and bool(MAM_ID)},
        "annas": {"name": "Anna's Archive", "type": "ebook", "available": annas_scraper is not None},
        "libgen": {"name": "Library Genesis", "type": "ebook", "available": libgen_scraper is not None},
        "openlib": {"name": "Open Library", "type": "ebook", "available": openlib_scraper is not None},
    }
    # Check Stacks
    stacks = await annas_scraper.get_stacks_status() if annas_scraper else {"available": False}
    sources["stacks"] = {"name": "Stacks (Download Manager)", "type": "download", "available": stacks.get("available", False)}
    return sources


async def _enrich_covers(results: list[AudiobookResult], query: str):
    """Fetch covers from Open Library for results that don't have them."""
    needs_cover = [r for r in results if not r.cover_url]
    if not needs_cover:
        return

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Search Open Library for the query to get OLIDs
        try:
            resp = await client.get(
                "https://openlibrary.org/search.json",
                params={"q": query, "limit": 20, "fields": "key,title,author_name,cover_i"},
            )
            if resp.status_code != 200:
                return
            ol_results = resp.json().get("docs", [])
        except Exception:
            return

        # Build a lookup of normalized title → cover_id
        cover_lookup: dict[str, int] = {}
        for doc in ol_results:
            cover_id = doc.get("cover_i")
            if not cover_id:
                continue
            title = doc.get("title", "").lower().strip()
            cover_lookup[title] = cover_id

        # Match results by title similarity
        for r in needs_cover:
            r_title = r.title.lower().strip()
            # Exact match
            if r_title in cover_lookup:
                r.cover_url = f"https://covers.openlibrary.org/b/id/{cover_lookup[r_title]}-M.jpg"
                continue
            # Substring match
            for ol_title, cover_id in cover_lookup.items():
                if ol_title in r_title or r_title in ol_title:
                    r.cover_url = f"https://covers.openlibrary.org/b/id/{cover_lookup[ol_title]}-M.jpg"
                    break


@app.get("/search", response_model=list[AudiobookResult])
async def search_books(
    q: str = Query(..., description="Search query"),
    content_type: str = Query("all", description="Filter: all, audiobook, ebook"),
):
    """Search for books across all sources."""
    if not scraper:
        raise HTTPException(status_code=500, detail="Scraper not initialized")

    tasks = []

    # Audiobook sources
    if content_type in ("all", "audiobook"):
        tasks.append(("abb", scraper.search(q)))
        if mam_scraper:
            tasks.append(("mam", mam_scraper.search(q)))

    # Ebook sources
    if content_type in ("all", "ebook"):
        if annas_scraper:
            tasks.append(("annas", annas_scraper.search(q)))
        if libgen_scraper:
            tasks.append(("libgen", libgen_scraper.search(q)))
        if openlib_scraper:
            tasks.append(("openlib", openlib_scraper.search(q)))
        # MAM also has ebooks — if searching "all" it's already included above
        if content_type == "ebook" and mam_scraper:
            tasks.append(("mam", mam_scraper.search(q)))

    # Run all searches in parallel
    coros = [t[1] for t in tasks]
    labels = [t[0] for t in tasks]
    all_results = await asyncio.gather(*coros, return_exceptions=True)

    results = []
    annas_failed = False
    for label, r in zip(labels, all_results):
        if isinstance(r, list):
            results.extend(r)
        elif isinstance(r, Exception):
            logger.warning(f"Search source {label} failed: {r}")
            if label == "annas":
                annas_failed = True

    # If Anna's Archive failed and LibGen returned no results, retry LibGen
    if annas_failed and not any(r.source == "libgen" for r in results) and libgen_scraper:
        logger.info("Anna's Archive failed — falling back to LibGen")
        try:
            libgen_results = await libgen_scraper.search(q)
            results.extend(libgen_results)
        except Exception as e:
            logger.warning(f"LibGen fallback also failed: {e}")

    # Filter by content_type if specified
    if content_type == "audiobook":
        results = [r for r in results if r.content_type == "audiobook"]
    elif content_type == "ebook":
        results = [r for r in results if r.content_type == "ebook"]

    # Deduplicate by title similarity (prefer sources with more metadata)
    results = _deduplicate_results(results)

    # Source priority: MAM > Anna's > LibGen > Open Library > ABB
    source_order = {"mam": 0, "annas": 1, "libgen": 2, "openlib": 3, "abb": 4}
    results.sort(key=lambda x: source_order.get(x.source, 5))

    # Enrich missing covers from Open Library (non-blocking, best-effort)
    try:
        await _enrich_covers(results, q)
    except Exception as e:
        logger.warning(f"Cover enrichment failed: {e}")

    return results


def _deduplicate_results(results: list[AudiobookResult]) -> list[AudiobookResult]:
    """Remove duplicate results across sources by normalizing titles."""
    seen: dict[str, AudiobookResult] = {}
    # Source priority for dedup (keep the one from the better source)
    source_priority = {"mam": 0, "annas": 1, "libgen": 2, "openlib": 3, "abb": 4}

    for r in results:
        # Normalize title for comparison
        norm = r.title.lower().strip()
        # Remove common suffixes
        for suffix in [" (epub)", " (pdf)", " (mobi)"]:
            norm = norm.replace(suffix, "")
        norm = _re.sub(r"[^a-z0-9\s]", "", norm).strip()

        if norm in seen:
            existing = seen[norm]
            # Keep the one from the higher-priority source
            if source_priority.get(r.source, 5) < source_priority.get(existing.source, 5):
                seen[norm] = r
            # Or keep the one with more metadata
            elif source_priority.get(r.source, 5) == source_priority.get(existing.source, 5):
                r_score = sum([bool(r.author), bool(r.cover_url), bool(r.size), bool(r.format)])
                e_score = sum([bool(existing.author), bool(existing.cover_url), bool(existing.size), bool(existing.format)])
                if r_score > e_score:
                    seen[norm] = r
        else:
            seen[norm] = r

    return list(seen.values())


@app.get("/audiobook/{book_id:path}", response_model=AudiobookDetail)
async def get_book_detail(book_id: str):
    """Get detailed information for a specific book."""
    if book_id.startswith("mam:"):
        if not mam_scraper:
            raise HTTPException(status_code=500, detail="MAM scraper not configured")
        torrent_id = book_id[4:]
        detail = await mam_scraper.get_detail(torrent_id)
    elif book_id.startswith("annas:"):
        if not annas_scraper:
            raise HTTPException(status_code=500, detail="Anna's Archive scraper not configured")
        md5 = book_id[6:]
        detail = await annas_scraper.get_detail(md5)
    elif book_id.startswith("libgen:"):
        if not libgen_scraper:
            raise HTTPException(status_code=500, detail="LibGen scraper not configured")
        md5 = book_id[7:]
        detail = await libgen_scraper.get_detail(md5)
    elif book_id.startswith("openlib:"):
        if not openlib_scraper:
            raise HTTPException(status_code=500, detail="Open Library scraper not configured")
        work_id = book_id[8:]
        detail = await openlib_scraper.get_detail(work_id)
    else:
        if not scraper:
            raise HTTPException(status_code=500, detail="Scraper not initialized")
        detail = await scraper.get_detail(book_id)

    if not detail:
        raise HTTPException(status_code=404, detail="Book not found or download link unavailable")

    return detail


@app.post("/download")
async def download_book(request: Request):
    """Route downloads based on content type: ebooks → CWA ingest, audiobooks → qBittorrent/Audiobookshelf."""
    try:
        body = await request.json()
        req = DownloadRequest(**body)
    except Exception as e:
        raw = await request.body()
        logger.error(f"Failed to parse download request: {e}, raw body: {raw[:200]}")
        raise HTTPException(status_code=400, detail=f"Invalid request body: {e}")

    author = req.author.strip() or "Unknown Author"
    title = req.title.strip() or "Unknown Title"

    if req.content_type == "ebook":
        return await _download_ebook(req, author, title)
    else:
        return await _download_audiobook(req, author, title)


async def _download_ebook(req: DownloadRequest, author: str, title: str):
    """Handle ebook downloads — route to CWA ingest folder via Stacks or direct download."""
    # Check CWA for duplicates (unless force)
    if not req.force:
        await _check_cwa_duplicate(title, author)

    if req.source in ("annas", "libgen"):
        # Extract MD5 for Stacks download
        md5 = None
        if req.source == "annas":
            md5_match = _re.search(r"/md5/([a-f0-9]+)", req.magnet_url, _re.IGNORECASE)
            if md5_match:
                md5 = md5_match.group(1)
        elif req.source == "libgen":
            md5_match = _re.search(r"md5=([a-f0-9]+)", req.magnet_url, _re.IGNORECASE)
            if not md5_match:
                md5_match = _re.search(r"/main/([a-f0-9]+)", req.magnet_url, _re.IGNORECASE)
            if md5_match:
                md5 = md5_match.group(1)

        # Try Stacks first (downloads directly to CWA ingest folder)
        if md5 and annas_scraper:
            stacks_result = await annas_scraper.download_via_stacks(md5)
            if stacks_result.get("success"):
                return {"status": "ok", "message": stacks_result["message"]}
            logger.warning(f"Stacks download failed: {stacks_result.get('error')} — falling back to direct download")

        # Fallback: direct HTTP download to CWA ingest
        if req.source == "annas" and annas_scraper:
            file_data, filename = await annas_scraper.download_file(req.magnet_url)
            if not file_data:
                raise HTTPException(status_code=502, detail="Failed to download ebook from Anna's Archive")

            if not filename:
                filename = f"{author} - {title}.epub"
            save_path = os.path.join(CWA_INGEST_PATH, filename)
            try:
                os.makedirs(CWA_INGEST_PATH, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(file_data)
                logger.info(f"Ebook saved to CWA ingest: {save_path}")
                return {"status": "ok", "message": f"Ebook saved → Calibre Library ({filename})"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to save ebook: {e}")

        elif req.source == "libgen":
            # LibGen: download from library.lol mirror
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    # library.lol page has the actual download link
                    r = await client.get(req.magnet_url)
                    r.raise_for_status()
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.text, "html.parser")
                    dl_link = soup.find("a", href=_re.compile(r"\.epub|\.pdf|\.mobi|cloudflare|get\.php", _re.IGNORECASE))
                    if dl_link:
                        dl_url = dl_link.get("href", "")
                        if not dl_url.startswith("http"):
                            dl_url = f"https://library.lol{dl_url}"
                        file_resp = await client.get(dl_url)
                        file_resp.raise_for_status()

                        filename = None
                        cd = file_resp.headers.get("content-disposition", "")
                        fname_match = _re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
                        if fname_match:
                            filename = fname_match.group(1).strip()
                        if not filename:
                            filename = f"{author} - {title}.epub"

                        save_path = os.path.join(CWA_INGEST_PATH, filename)
                        os.makedirs(CWA_INGEST_PATH, exist_ok=True)
                        with open(save_path, "wb") as f:
                            f.write(file_resp.content)
                        logger.info(f"LibGen ebook saved to CWA ingest: {save_path}")
                        return {"status": "ok", "message": f"Ebook saved → Calibre Library ({filename})"}
                    else:
                        raise HTTPException(status_code=502, detail="Could not find download link on LibGen mirror page")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"LibGen download failed: {e}")

    elif req.source == "mam":
        # MAM ebook: download .torrent → qBittorrent with CWA ingest path
        if not mam_scraper:
            raise HTTPException(status_code=500, detail="MAM scraper not configured")

        tid_match = _re.search(r"tid=(\d+)", req.magnet_url) or _re.search(r"/download\.php/(\d+)", req.magnet_url)
        if not tid_match:
            raise HTTPException(status_code=400, detail="Invalid MAM torrent URL")

        torrent_data = await mam_scraper.download_torrent_file(tid_match.group(1))
        if not torrent_data:
            raise HTTPException(status_code=502, detail="Failed to download .torrent file from MAM")

        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            resp = await client.post(
                f"{QBITTORRENT_URL}/api/v2/torrents/add",
                files={"torrents": ("ebook.torrent", torrent_data, "application/x-bittorrent")},
                data={
                    "savepath": CWA_INGEST_PATH,
                    "category": "ebooks",
                    "tags": "mam",
                },
            )
            if resp.status_code != 200 or resp.text.strip().lower() != "ok.":
                raise HTTPException(status_code=502, detail=f"qBittorrent rejected torrent: {resp.text}")

        return {"status": "ok", "message": f"Ebook download started → Calibre Library"}

    elif req.source == "openlib":
        # Open Library: redirect to reading/borrowing page
        return {"status": "ok", "message": f"Open Library book — open in browser to borrow/read", "redirect": req.magnet_url}

    raise HTTPException(status_code=400, detail=f"Unsupported ebook source: {req.source}")


async def _download_audiobook(req: DownloadRequest, author: str, title: str):
    """Handle audiobook downloads — existing flow via qBittorrent → Audiobookshelf."""
    save_path = f"/audiobooks/{author}/{title}"

    # Check Audiobookshelf for duplicates (unless force)
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
            login_resp = await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            if login_resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"qBittorrent login failed: {login_resp.status_code}")

            if is_mam_torrent:
                if not mam_scraper:
                    raise HTTPException(status_code=500, detail="MAM scraper not configured")

                tid_match = _re.search(r"tid=(\d+)", req.magnet_url) or _re.search(r"/download\.php/(\d+)", req.magnet_url)
                if not tid_match:
                    raise HTTPException(status_code=400, detail="Invalid MAM torrent URL")

                torrent_data = await mam_scraper.download_torrent_file(tid_match.group(1))
                if not torrent_data:
                    raise HTTPException(status_code=502, detail="Failed to download .torrent file from MAM")

                resp = await client.post(
                    f"{QBITTORRENT_URL}/api/v2/torrents/add",
                    files={"torrents": ("audiobook.torrent", torrent_data, "application/x-bittorrent")},
                    data={
                        "savepath": save_path,
                        "category": "audiobooks",
                        "tags": "mam",
                    },
                )
            else:
                hash_match = _re.search(r"btih:([a-fA-F0-9]{40})", req.magnet_url)
                if hash_match:
                    info_hash = hash_match.group(1).lower()
                    await client.post(
                        f"{QBITTORRENT_URL}/api/v2/torrents/delete",
                        data={"hashes": info_hash, "deleteFiles": "false"},
                    )

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

        asyncio.create_task(_poll_and_scan(req.magnet_url, author, title, save_path))
        return {"status": "ok", "message": f"Download started → Audiobookshelf ({save_path})"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to add torrent: {e}")


async def _check_cwa_duplicate(title: str, author: str):
    """Check Calibre-Web for duplicate ebooks."""
    # CWA doesn't have a reliable REST API for search, so we check the ingest folder
    # for files with similar names
    try:
        if os.path.exists(CWA_INGEST_PATH):
            for f in os.listdir(CWA_INGEST_PATH):
                fname = f.lower()
                if title.lower().replace(" ", "") in fname.replace(" ", "").replace("-", "").replace("_", ""):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Ebook already in ingest queue: {f}"
                    )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"CWA duplicate check failed (proceeding anyway): {e}")


async def _poll_and_scan(magnet_url: str, author: str, title: str, save_path: str = ""):
    """Poll qBittorrent until download completes, then trigger Audiobookshelf scan."""
    info_hash = None
    hash_match = _re.search(r"btih:([a-fA-F0-9]{40})", magnet_url)
    if hash_match:
        info_hash = hash_match.group(1).lower()

    poll_interval = 30
    max_polls = 120

    async with httpx.AsyncClient(timeout=15.0) as client:
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
    if not AUDIOBOOKSHELF_TOKEN:
        return
    try:
        resp = await client.get(
            f"{AUDIOBOOKSHELF_URL}/api/libraries",
            headers={"Authorization": f"Bearer {AUDIOBOOKSHELF_TOKEN}"},
        )
        for lib in resp.json().get("libraries", []):
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
    """Get status of all book downloads from qBittorrent."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
            )
            # Get both audiobook and ebook category torrents
            results = []
            for category in ("audiobooks", "ebooks"):
                resp = await client.get(
                    f"{QBITTORRENT_URL}/api/v2/torrents/info",
                    params={"category": category},
                )
                for t in resp.json():
                    results.append({
                        "hash": t["hash"],
                        "name": t["name"],
                        "progress": t["progress"],
                        "size": t["total_size"],
                        "downloaded": t["downloaded"],
                        "uploaded": t["uploaded"],
                        "speed": t["dlspeed"],
                        "up_speed": t["upspeed"],
                        "eta": t["eta"],
                        "state": t["state"],
                        "save_path": t["save_path"],
                        "category": category,
                        "ratio": round(t["ratio"], 2),
                        "tags": t.get("tags", ""),
                        "tracker": t.get("tracker", ""),
                    })
            return results
    except Exception as e:
        return []


@app.delete("/downloads/{torrent_hash}")
async def delete_download(torrent_hash: str, delete_files: bool = False):
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
            if delete_files:
                await _trigger_audiobookshelf_scan(client)
            return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to delete torrent: {e}")


async def _periodic_sync():
    while True:
        await asyncio.sleep(300)
        try:
            await _run_sync()
        except Exception as e:
            logger.warning(f"Sync error: {e}")


async def _run_sync():
    if not AUDIOBOOKSHELF_TOKEN:
        return

    changed = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{QBITTORRENT_URL}/api/v2/auth/login",
            data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS},
        )
        resp = await client.get(
            f"{QBITTORRENT_URL}/api/v2/torrents/info",
            params={"category": "audiobooks"},
        )
        torrents = resp.json()

        for t in torrents:
            save_path = t.get("save_path", "")
            if not save_path or not save_path.startswith("/audiobooks/"):
                continue
            # Never remove MAM torrents — keep seeding for ratio
            tracker = t.get("tracker", "")
            tags = t.get("tags", "")
            if "myanonamouse" in tracker or "mam" in tags.lower():
                continue
            if not os.path.exists(save_path):
                logger.info(f"Sync: removing orphaned torrent '{t['name']}' (path gone: {save_path})")
                await client.post(
                    f"{QBITTORRENT_URL}/api/v2/torrents/delete",
                    data={"hashes": t["hash"], "deleteFiles": "true"},
                )
                changed = True

    if changed:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await _trigger_audiobookshelf_scan(client)


@app.post("/sync")
async def trigger_sync():
    try:
        await _run_sync()
        return {"status": "ok", "message": "Sync completed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@app.get("/files/{path:path}")
async def list_files(path: str):
    # Allow access under /audiobooks/ and /cwa-book-ingest/
    for base in ("/audiobooks", CWA_INGEST_PATH):
        full_path = os.path.normpath(os.path.join(base, path))
        if full_path.startswith(base) and os.path.exists(full_path):
            if os.path.isfile(full_path):
                return FileResponse(full_path, filename=os.path.basename(full_path))
            files = []
            for entry in sorted(os.listdir(full_path)):
                entry_path = os.path.join(full_path, entry)
                rel_path = os.path.relpath(entry_path, base)
                if os.path.isfile(entry_path):
                    files.append({"name": entry, "size": os.path.getsize(entry_path), "path": rel_path})
                elif os.path.isdir(entry_path):
                    files.append({"name": entry + "/", "size": 0, "path": rel_path})
            return files

    raise HTTPException(status_code=404, detail="Path not found")


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the web UI."""
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Book Search</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=Source+Sans+3:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-deep: #0f0e0c;
            --bg-main: #171613;
            --bg-card: #1e1d19;
            --bg-card-hover: #252420;
            --bg-input: #121110;
            --border: #2a2824;
            --border-hover: #3d3930;
            --text: #c8c0b0;
            --text-dim: #7a7468;
            --text-bright: #ede6d6;
            --accent-amber: #c9953c;
            --accent-amber-dim: #8a6828;
            --accent-amber-glow: rgba(201, 149, 60, 0.12);
            --accent-green: #5a8a50;
            --accent-red: #a04040;
            --accent-purple: #7a6aaa;
            --badge-mam: #6a5acd;
            --badge-abb: #4a7a8a;
            --badge-annas: #8a6a3a;
            --badge-libgen: #3a6a8a;
            --badge-openlib: #5a7a5a;
            --badge-audiobook: #3a6a5a;
            --badge-ebook: #6a4a3a;
            --font-display: 'Playfair Display', Georgia, serif;
            --font-body: 'Source Sans 3', 'Segoe UI', sans-serif;
            --radius: 6px;
            --radius-lg: 10px;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: var(--font-body);
            background: var(--bg-deep);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
        }

        /* Subtle texture overlay */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            background: url("data:image/svg+xml,%3Csvg width='60' height='60' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence baseFrequency='0.65' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
            pointer-events: none;
            z-index: 0;
        }

        .page { position: relative; z-index: 1; }

        /* ── Header ── */
        .header {
            border-bottom: 1px solid var(--border);
            padding: 24px 0 20px;
            background: linear-gradient(180deg, rgba(201,149,60,0.04) 0%, transparent 100%);
        }

        .header-inner {
            max-width: 1100px;
            margin: 0 auto;
            padding: 0 24px;
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 16px;
        }

        .logo {
            font-family: var(--font-display);
            font-size: 28px;
            font-weight: 700;
            color: var(--text-bright);
            letter-spacing: -0.5px;
        }

        .logo span {
            color: var(--accent-amber);
        }

        .nav-links {
            display: flex;
            gap: 20px;
            align-items: center;
        }

        .nav-links a {
            font-size: 13px;
            font-weight: 500;
            color: var(--text-dim);
            text-decoration: none;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            transition: color 0.2s;
            padding: 4px 0;
            border-bottom: 1px solid transparent;
        }

        .nav-links a:hover {
            color: var(--accent-amber);
            border-bottom-color: var(--accent-amber-dim);
        }

        /* ── Main ── */
        .container {
            max-width: 1100px;
            margin: 0 auto;
            padding: 32px 24px;
        }

        /* ── Search ── */
        .search-area {
            margin-bottom: 32px;
        }

        .search-box {
            display: flex;
            gap: 0;
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            overflow: hidden;
            background: var(--bg-input);
            transition: border-color 0.2s;
        }

        .search-box:focus-within {
            border-color: var(--accent-amber-dim);
            box-shadow: 0 0 0 3px var(--accent-amber-glow);
        }

        .search-box input {
            flex: 1;
            padding: 14px 20px;
            font-size: 16px;
            font-family: var(--font-body);
            border: none;
            background: transparent;
            color: var(--text-bright);
            outline: none;
        }

        .search-box input::placeholder {
            color: var(--text-dim);
            font-style: italic;
        }

        .search-box button {
            padding: 14px 28px;
            font-family: var(--font-body);
            font-size: 14px;
            font-weight: 600;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            border: none;
            background: var(--accent-amber);
            color: var(--bg-deep);
            cursor: pointer;
            transition: background 0.2s;
        }

        .search-box button:hover { background: #d4a044; }
        .search-box button:disabled { background: var(--border); color: var(--text-dim); cursor: not-allowed; }

        /* ── Filter Tabs ── */
        .filter-tabs {
            display: flex;
            gap: 2px;
            margin-top: 16px;
            background: var(--bg-input);
            border-radius: var(--radius);
            padding: 3px;
            width: fit-content;
        }

        .filter-tab {
            padding: 7px 18px;
            font-size: 13px;
            font-weight: 500;
            font-family: var(--font-body);
            border: none;
            border-radius: 4px;
            background: transparent;
            color: var(--text-dim);
            cursor: pointer;
            transition: all 0.2s;
            letter-spacing: 0.3px;
        }

        .filter-tab:hover { color: var(--text); }

        .filter-tab.active {
            background: var(--accent-amber);
            color: var(--bg-deep);
            font-weight: 600;
        }

        /* ── Sources Bar ── */
        .sources-bar {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 16px;
            align-items: center;
        }

        .source-chip {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.3px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            color: var(--text-dim);
        }

        .source-chip.active {
            border-color: var(--accent-amber-dim);
            color: var(--text);
        }

        .source-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--accent-red);
        }

        .source-chip.active .source-dot {
            background: var(--accent-green);
        }

        /* ── Status ── */
        .status {
            padding: 12px 16px;
            margin-bottom: 20px;
            border-radius: var(--radius);
            display: none;
            font-size: 14px;
            border-left: 3px solid;
        }

        .status.success { background: rgba(90,138,80,0.1); border-color: var(--accent-green); color: #a0d090; }
        .status.error { background: rgba(160,64,64,0.1); border-color: var(--accent-red); color: #e0a0a0; }
        .status.info { background: var(--accent-amber-glow); border-color: var(--accent-amber-dim); color: var(--accent-amber); }

        /* ── Results Grid ── */
        .results {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 16px;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            overflow: hidden;
            transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s;
            display: flex;
            flex-direction: column;
        }

        .card:hover {
            transform: translateY(-3px);
            border-color: var(--border-hover);
            box-shadow: 0 8px 24px rgba(0,0,0,0.3);
        }

        .card-image-wrap {
            height: 160px;
            overflow: hidden;
            background: var(--bg-input);
            position: relative;
        }

        .card-image {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .card-image-placeholder {
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 48px;
            background: linear-gradient(135deg, var(--bg-input) 0%, var(--bg-card) 100%);
            color: var(--text-dim);
            opacity: 0.6;
        }

        .card-badges {
            position: absolute;
            top: 8px;
            left: 8px;
            display: flex;
            gap: 5px;
        }

        .badge {
            padding: 3px 8px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            font-family: var(--font-body);
        }

        .badge-mam { background: var(--badge-mam); color: #fff; }
        .badge-abb { background: var(--badge-abb); color: #fff; }
        .badge-annas { background: var(--badge-annas); color: #fff; }
        .badge-libgen { background: var(--badge-libgen); color: #fff; }
        .badge-openlib { background: var(--badge-openlib); color: #fff; }
        .badge-audiobook { background: var(--badge-audiobook); color: #d0ffe0; }
        .badge-ebook { background: var(--badge-ebook); color: #ffe0d0; }

        .card-body {
            padding: 16px;
            flex: 1;
            display: flex;
            flex-direction: column;
        }

        .card-title {
            font-family: var(--font-display);
            font-size: 17px;
            font-weight: 600;
            color: var(--text-bright);
            margin-bottom: 8px;
            line-height: 1.3;
        }

        .card-meta {
            font-size: 13px;
            color: var(--text-dim);
            margin-bottom: 3px;
        }

        .card-meta strong {
            color: var(--text);
            font-weight: 500;
        }

        .card-actions {
            margin-top: auto;
            padding-top: 12px;
        }

        .btn-download {
            width: 100%;
            padding: 10px;
            font-family: var(--font-body);
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.3px;
            border: 1px solid var(--accent-amber-dim);
            border-radius: var(--radius);
            background: transparent;
            color: var(--accent-amber);
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-download:hover {
            background: var(--accent-amber);
            color: var(--bg-deep);
        }

        /* ── Modal ── */
        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.75);
            backdrop-filter: blur(4px);
            z-index: 1000;
        }

        .modal {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            max-width: 480px;
            width: 92%;
            padding: 28px;
        }

        .modal h2 {
            font-family: var(--font-display);
            font-size: 22px;
            color: var(--text-bright);
            margin-bottom: 20px;
        }

        .modal-field {
            margin-bottom: 14px;
        }

        .modal-field label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-dim);
            margin-bottom: 5px;
        }

        .modal-field input {
            width: 100%;
            padding: 10px 14px;
            font-family: var(--font-body);
            font-size: 14px;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            background: var(--bg-input);
            color: var(--text-bright);
            outline: none;
            transition: border-color 0.2s;
        }

        .modal-field input:focus {
            border-color: var(--accent-amber-dim);
        }

        .modal-destination {
            margin: 16px 0;
            padding: 10px 14px;
            background: var(--accent-amber-glow);
            border: 1px solid var(--accent-amber-dim);
            border-radius: var(--radius);
            font-size: 13px;
            color: var(--accent-amber);
        }

        .modal-actions {
            display: flex;
            gap: 8px;
            margin-top: 20px;
            flex-wrap: wrap;
        }

        .modal-actions button {
            flex: 1;
            min-width: 100px;
            padding: 10px 16px;
            font-family: var(--font-body);
            font-size: 13px;
            font-weight: 600;
            border: none;
            border-radius: var(--radius);
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-cancel { background: var(--border); color: var(--text); }
        .btn-cancel:hover { background: var(--border-hover); }

        .btn-magnet { background: var(--accent-purple); color: #fff; }
        .btn-magnet:hover { opacity: 0.9; }

        .btn-confirm { background: var(--accent-amber); color: var(--bg-deep); }
        .btn-confirm:hover { background: #d4a044; }

        /* ── Downloads Section ── */
        .section-heading {
            font-family: var(--font-display);
            font-size: 22px;
            color: var(--text-bright);
            margin: 40px 0 20px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border);
        }

        .dl-list {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .dl-item {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 16px;
        }

        .dl-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
            flex-wrap: wrap;
        }

        .dl-name {
            font-family: var(--font-display);
            font-size: 16px;
            font-weight: 600;
            color: var(--text-bright);
            flex: 1;
            min-width: 200px;
        }

        .dl-meta {
            font-size: 13px;
            color: var(--text-dim);
            margin-bottom: 3px;
        }

        .progress-track {
            width: 100%;
            height: 22px;
            background: var(--bg-input);
            border-radius: 4px;
            overflow: hidden;
            margin: 8px 0;
            border: 1px solid var(--border);
        }

        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--accent-amber-dim), var(--accent-amber));
            transition: width 0.4s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 700;
            color: var(--bg-deep);
        }

        .progress-fill.complete {
            background: linear-gradient(90deg, #3a6a30, var(--accent-green));
        }

        .dl-state {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.3px;
            text-transform: uppercase;
        }

        .state-downloading { background: rgba(90,159,212,0.15); color: #8ac0e8; }
        .state-seeding { background: rgba(90,138,80,0.15); color: #90c880; }
        .state-paused { background: rgba(180,160,60,0.15); color: #d0c870; }
        .state-error { background: rgba(160,64,64,0.15); color: #e0a0a0; }
        .state-stalled { background: rgba(180,140,60,0.15); color: #d0b870; }
        .state-queued { background: rgba(100,100,100,0.15); color: #aaa; }

        .dl-actions {
            display: flex;
            gap: 6px;
            margin-top: 10px;
            align-items: center;
            flex-wrap: wrap;
        }

        .btn-sm {
            padding: 5px 12px;
            font-family: var(--font-body);
            font-size: 12px;
            font-weight: 600;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: opacity 0.2s;
        }

        .btn-sm:hover { opacity: 0.8; }
        .btn-browse { background: var(--accent-green); color: #fff; }
        .btn-remove { background: var(--border); color: var(--text); }
        .btn-delete { background: var(--accent-red); color: #fff; }

        .no-downloads {
            text-align: center;
            padding: 40px;
            color: var(--text-dim);
            font-style: italic;
        }

        .files-panel {
            display: none;
            margin-top: 10px;
            padding: 10px;
            background: var(--bg-input);
            border-radius: var(--radius);
            font-size: 13px;
        }

        .destination-tag {
            font-size: 11px;
            font-weight: 600;
            padding: 2px 7px;
            border-radius: 3px;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .dest-audiobookshelf { background: rgba(90,138,80,0.2); color: #90c880; }
        .dest-calibre { background: rgba(201,149,60,0.2); color: var(--accent-amber); }

        /* ── Responsive ── */
        @media (max-width: 600px) {
            .header-inner { flex-direction: column; align-items: center; gap: 12px; }
            .results { grid-template-columns: 1fr; }
            .modal { width: 96%; padding: 20px; }
            .logo { font-size: 24px; }
        }

        /* ── Animation ── */
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .card { animation: fadeIn 0.3s ease both; }
        .card:nth-child(2) { animation-delay: 0.04s; }
        .card:nth-child(3) { animation-delay: 0.08s; }
        .card:nth-child(4) { animation-delay: 0.12s; }
        .card:nth-child(5) { animation-delay: 0.16s; }
        .card:nth-child(6) { animation-delay: 0.20s; }
    </style>
</head>
<body>
<div class="page">
    <header class="header">
        <div class="header-inner">
            <div class="logo">Book<span>Search</span></div>
            <nav class="nav-links">
                <a href="https://audiobookshelf.viktorbarzin.me" target="_blank">Audiobookshelf</a>
                <a href="https://calibre.viktorbarzin.me" target="_blank">Calibre Library</a>
                <a href="https://stacks.viktorbarzin.me" target="_blank">Stacks</a>
                <a href="#" onclick="showMamSetup(); return false;" id="mamLink" style="opacity:0.5">MAM Setup</a>
            </nav>
        </div>
    </header>

    <div class="container">
        <div class="search-area">
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Search for books...">
                <button id="searchBtn" onclick="search()">Search</button>
            </div>
            <div class="filter-tabs">
                <button class="filter-tab active" data-filter="all" onclick="setFilter(this)">All</button>
                <button class="filter-tab" data-filter="audiobook" onclick="setFilter(this)">Audiobooks</button>
                <button class="filter-tab" data-filter="ebook" onclick="setFilter(this)">Ebooks</button>
            </div>
        </div>

        <div id="sourcesBar" class="sources-bar"></div>
        <div id="status" class="status"></div>
        <div id="results" class="results"></div>

        <h2 class="section-heading">Active Downloads</h2>
        <div id="downloads" class="dl-list"></div>
    </div>
</div>

<div id="downloadModal" class="modal-overlay">
    <div class="modal">
        <h2>Download Book</h2>
        <div class="modal-field">
            <label for="authorInput">Author</label>
            <input type="text" id="authorInput">
        </div>
        <div class="modal-field">
            <label for="titleInput">Title</label>
            <input type="text" id="titleInput">
        </div>
        <div id="modalDestination" class="modal-destination"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal()">Cancel</button>
            <button class="btn-magnet" onclick="copyMagnetLink()">Copy Link</button>
            <button class="btn-confirm" onclick="confirmDownload()">Send to Library</button>
        </div>
    </div>
</div>

<script>
    let currentMagnetUrl = null;
    let currentContentType = 'audiobook';
    let currentSource = '';
    let activeFilter = 'all';

    function showStatus(message, type) {
        const el = document.getElementById('status');
        el.textContent = message;
        el.className = `status ${type}`;
        el.style.display = 'block';
        if (type !== 'info' || !message.includes('Searching')) {
            setTimeout(() => { el.style.display = 'none'; }, 5000);
        }
    }

    function showStatusHTML(html, type) {
        const el = document.getElementById('status');
        el.innerHTML = html;
        el.className = `status ${type}`;
        el.style.display = 'block';
    }

    function formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function formatSpeed(bps) {
        if (bps === 0) return '0 B/s';
        const k = 1024, sizes = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
        const i = Math.floor(Math.log(bps) / Math.log(k));
        return parseFloat((bps / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function formatETA(s) {
        if (s === 8640000 || s < 0) return '?';
        if (s === 0) return 'Done';
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        return h > 0 ? `${h}h ${m}m` : `${m}m`;
    }

    function stateClass(s) {
        const m = {downloading:'state-downloading', stalledDL:'state-stalled', uploading:'state-seeding',
            stalledUP:'state-seeding', pausedDL:'state-paused', pausedUP:'state-paused',
            queuedDL:'state-queued', queuedUP:'state-queued', error:'state-error',
            checkingUP:'state-queued', checkingDL:'state-queued'};
        return m[s] || 'state-queued';
    }

    function stateText(s) {
        const m = {downloading:'Downloading', stalledDL:'Stalled', uploading:'Seeding',
            stalledUP:'Seeding', pausedDL:'Paused', pausedUP:'Paused',
            queuedDL:'Queued', queuedUP:'Queued', error:'Error',
            checkingUP:'Checking', checkingDL:'Checking'};
        return m[s] || s;
    }

    function sourceBadge(source) {
        const m = {
            mam: ['MAM', 'badge-mam'],
            abb: ['ABB', 'badge-abb'],
            annas: ["Anna's", 'badge-annas'],
            libgen: ['LibGen', 'badge-libgen'],
            openlib: ['OpenLib', 'badge-openlib'],
        };
        const [label, cls] = m[source] || [source, 'badge-abb'];
        return `<span class="badge ${cls}">${label}</span>`;
    }

    function typeBadge(ct) {
        return ct === 'ebook'
            ? '<span class="badge badge-ebook">Ebook</span>'
            : '<span class="badge badge-audiobook">Audiobook</span>';
    }

    function setFilter(btn) {
        document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        activeFilter = btn.dataset.filter;
        // Re-search if there's a query
        const q = document.getElementById('searchInput').value.trim();
        if (q) search();
    }

    async function search() {
        const query = document.getElementById('searchInput').value.trim();
        if (!query) { showStatus('Enter a search query', 'error'); return; }

        const btn = document.getElementById('searchBtn');
        btn.disabled = true;
        showStatus('Searching...', 'info');

        try {
            const url = `/search?q=${encodeURIComponent(query)}&content_type=${activeFilter}`;
            const resp = await fetch(url);
            if (!resp.ok) throw new Error('Search failed');
            const results = await resp.json();
            displayResults(results);
            showStatus(results.length === 0 ? 'No results found' : `Found ${results.length} results`, results.length ? 'success' : 'info');
        } catch (e) {
            showStatus('Search failed: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
        }
    }

    function esc(text) {
        const d = document.createElement('div');
        d.textContent = text;
        return d.innerHTML;
    }

    function displayResults(results) {
        const el = document.getElementById('results');
        if (!results.length) { el.innerHTML = ''; return; }

        el.innerHTML = results.map(b => {
            const title = esc(b.title);
            const author = esc(b.author || '');
            const dest = b.source === 'openlib' ? 'Open Library' : (b.content_type === 'ebook' ? 'Calibre Library' : 'Audiobookshelf');

            return `
                <div class="card">
                    <div class="card-image-wrap">
                        ${b.cover_url ? `<img src="${b.cover_url}" alt="" class="card-image" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="card-image-placeholder" style="display:none">${b.content_type === 'ebook' ? '&#128214;' : '&#127911;'}</div>` : `<div class="card-image-placeholder">${b.content_type === 'ebook' ? '&#128214;' : '&#127911;'}</div>`}
                        <div class="card-badges">
                            ${sourceBadge(b.source)}
                            ${typeBadge(b.content_type)}
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="card-title">${title}</div>
                        ${b.author ? `<div class="card-meta"><strong>Author:</strong> ${author}</div>` : ''}
                        ${b.narrator ? `<div class="card-meta"><strong>Narrator:</strong> ${esc(b.narrator)}</div>` : ''}
                        ${b.format ? `<div class="card-meta"><strong>Format:</strong> ${esc(b.format)}</div>` : ''}
                        ${b.size ? `<div class="card-meta"><strong>Size:</strong> ${esc(b.size)}</div>` : ''}
                        <div class="card-actions">
                            <button class="btn-download"
                                data-book-id="${esc(b.id)}"
                                data-book-author="${author}"
                                data-book-title="${title}"
                                data-content-type="${b.content_type}"
                                data-source="${b.source}"
                                onclick="${b.source === 'openlib' ? `window.open('${esc(b.url)}', '_blank')` : 'prepareDownload(this)'}">
                                ${b.source === 'openlib' ? 'Read on Open Library &nearr;' : `Download &rarr; ${dest}`}
                            </button>
                        </div>
                    </div>
                </div>`;
        }).join('');
    }

    async function prepareDownload(btn) {
        const bookId = btn.dataset.bookId;
        currentContentType = btn.dataset.contentType;
        currentSource = btn.dataset.source;
        showStatus('Fetching details...', 'info');

        try {
            const resp = await fetch(`/audiobook/${encodeURIComponent(bookId)}`);
            if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
            const detail = await resp.json();
            if (!detail.magnet_url) throw new Error('No download link');

            currentMagnetUrl = detail.magnet_url;
            currentContentType = detail.content_type || currentContentType;
            currentSource = detail.source || currentSource;

            document.getElementById('authorInput').value = detail.author || btn.dataset.bookAuthor || '';
            document.getElementById('titleInput').value = detail.title || btn.dataset.bookTitle || '';

            const dest = currentContentType === 'ebook' ? 'Calibre Library' : 'Audiobookshelf';
            const via = currentContentType === 'ebook'
                ? (currentSource === 'annas' || currentSource === 'libgen' ? 'via Stacks → CWA ingest' : 'CWA auto-ingest')
                : 'qBittorrent';
            document.getElementById('modalDestination').innerHTML =
                `&#8594; <strong>${dest}</strong> <span style="opacity:0.7">(${via})</span>`;

            document.getElementById('downloadModal').style.display = 'block';
            document.getElementById('status').style.display = 'none';
        } catch (e) {
            showStatus('Failed to get details: ' + e.message, 'error');
        }
    }

    function closeModal() {
        document.getElementById('downloadModal').style.display = 'none';
        currentMagnetUrl = null;
    }

    async function copyMagnetLink() {
        if (!currentMagnetUrl) return;
        try {
            await navigator.clipboard.writeText(currentMagnetUrl);
            showStatus('Link copied!', 'success');
            closeModal();
        } catch (e) {
            prompt('Copy this link:', currentMagnetUrl);
        }
    }

    async function confirmDownload(force = false) {
        const author = document.getElementById('authorInput').value.trim();
        const title = document.getElementById('titleInput').value.trim();
        if (!title) { showStatus('Title is required', 'error'); return; }
        if (!currentMagnetUrl) { showStatus('No download link', 'error'); return; }

        const magnetUrl = currentMagnetUrl;
        const contentType = currentContentType;
        const source = currentSource;
        closeModal();
        showStatus('Starting download...', 'info');

        try {
            const body = { magnet_url: magnetUrl, author, title, force, content_type: contentType, source };
            const resp = await fetch('/download', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });

            if (resp.status === 409) {
                const err = await resp.json();
                const detail = err.detail || 'Book already exists';
                showStatusHTML(`${esc(detail)} <button onclick="redownload('${btoa(magnetUrl)}','${btoa(author)}','${btoa(title)}','${contentType}','${source}')" style="margin-left:10px;padding:4px 12px;background:var(--accent-amber-dim);border:none;border-radius:4px;color:var(--text-bright);cursor:pointer;font-size:13px;">Download Anyway</button>`, 'info');
                return;
            }

            if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
            const result = await resp.json();
            showStatus(result.message || 'Download started!', 'success');
            refreshDownloads();
        } catch (e) {
            showStatus('Download failed: ' + e.message, 'error');
        }
    }

    async function redownload(mB64, aB64, tB64, ct, src) {
        currentMagnetUrl = atob(mB64);
        currentContentType = ct;
        currentSource = src;
        document.getElementById('authorInput').value = atob(aB64);
        document.getElementById('titleInput').value = atob(tB64);
        await confirmDownload(true);
    }

    async function cancelDownload(hash, deleteFiles) {
        const action = deleteFiles ? 'delete with files' : 'remove';
        if (!confirm(`Are you sure you want to ${action} this download?`)) return;
        try {
            await fetch(`/downloads/${hash}?delete_files=${deleteFiles}`, {method: 'DELETE'});
            showStatus('Download removed', 'success');
            refreshDownloads();
        } catch (e) {
            showStatus('Failed: ' + e.message, 'error');
        }
    }

    async function refreshDownloads() {
        try {
            const resp = await fetch('/downloads');
            if (!resp.ok) throw new Error('Failed');
            displayDownloads(await resp.json());
        } catch (e) { /* silent */ }
    }

    function displayDownloads(downloads) {
        const el = document.getElementById('downloads');
        if (!downloads.length) {
            el.innerHTML = '<div class="no-downloads">No active downloads</div>';
            return;
        }

        el.innerHTML = downloads.map(dl => {
            const pct = (dl.progress * 100).toFixed(1);
            const done = dl.progress >= 1.0;
            const isEbook = dl.category === 'ebooks';
            const destClass = isEbook ? 'dest-calibre' : 'dest-audiobookshelf';
            const destLabel = isEbook ? 'Calibre' : 'Audiobookshelf';

            return `
                <div class="dl-item">
                    <div class="dl-header">
                        <div class="dl-name">${esc(dl.name)}</div>
                        <span class="destination-tag ${destClass}">${destLabel}</span>
                    </div>
                    <div class="progress-track">
                        <div class="progress-fill ${done ? 'complete' : ''}" style="width:${pct}%">${pct}%</div>
                    </div>
                    <div class="dl-meta">Size: ${formatBytes(dl.size)} &middot; Down: ${formatBytes(dl.downloaded)} &middot; Up: ${formatBytes(dl.uploaded || 0)}</div>
                    <div class="dl-meta">${dl.progress < 1 ? 'DL: ' + formatSpeed(dl.speed) + ' &middot; ETA: ' + formatETA(dl.eta) : ''} ${dl.up_speed > 0 ? 'UL: ' + formatSpeed(dl.up_speed) : ''} &middot; Ratio: <strong style="color:${dl.ratio >= 1 ? 'var(--accent-green)' : 'var(--accent-amber)'}">${dl.ratio.toFixed(2)}</strong></div>
                    <div class="dl-meta">Path: ${esc(dl.save_path)}</div>
                    <div class="dl-actions">
                        <span class="dl-state ${stateClass(dl.state)}">${stateText(dl.state)}</span>
                        ${done ? `<button class="btn-sm btn-browse" onclick="browseFiles('${encodeURIComponent(dl.save_path.replace(/^\\/audiobooks\\//, ''))}')">Browse</button>` : ''}
                        <button class="btn-sm btn-remove" onclick="cancelDownload('${dl.hash}',false)">Remove</button>
                        <button class="btn-sm btn-delete" onclick="cancelDownload('${dl.hash}',true)">Delete + Files</button>
                    </div>
                    <div id="files-${dl.hash}" class="files-panel"></div>
                </div>`;
        }).join('');
    }

    async function browseFiles(relativePath) {
        try {
            const resp = await fetch(`/files/${relativePath}`);
            if (!resp.ok) throw new Error(`${resp.status}`);
            const files = await resp.json();

            const allPanels = document.querySelectorAll('.files-panel');
            let target = null;
            for (const p of allPanels) {
                const item = p.closest('.dl-item');
                if (item && item.querySelector(`[onclick*="${CSS.escape(relativePath)}"]`)) {
                    target = p; break;
                }
            }
            if (!target) return;

            if (target.style.display !== 'none' && target.style.display !== '') {
                target.style.display = 'none'; return;
            }

            target.innerHTML = files.length === 0 ? '<em style="color:var(--text-dim)">No files</em>' :
                files.map(f => {
                    const icon = f.name.endsWith('/') ? '&#128193;' : '&#128196;';
                    const link = f.name.endsWith('/')
                        ? `<a href="javascript:void(0)" onclick="browseFiles('${encodeURIComponent(f.path)}')" style="color:var(--accent-amber);text-decoration:none">${f.name}</a>`
                        : `<a href="/files/${encodeURIComponent(f.path)}" download style="color:var(--accent-amber);text-decoration:none">${f.name}</a> <span style="color:var(--text-dim)">(${formatBytes(f.size)})</span>`;
                    return `<div style="padding:3px 0">${icon} ${link}</div>`;
                }).join('');
            target.style.display = 'block';
        } catch (e) {
            showStatus('Failed to list files: ' + e.message, 'error');
        }
    }

    document.getElementById('searchInput').addEventListener('keypress', e => {
        if (e.key === 'Enter') search();
    });

    document.getElementById('downloadModal').addEventListener('click', e => {
        if (e.target.id === 'downloadModal') closeModal();
    });

    setInterval(refreshDownloads, 10000);
    refreshDownloads();

    // MAM setup
    async function checkMamStatus() {
        try {
            const r = await fetch('/mam-status');
            const data = await r.json();
            const link = document.getElementById('mamLink');
            if (data.authenticated) {
                link.style.opacity = '1';
                link.style.color = 'var(--accent-green)';
                link.title = 'MAM: Active';
            } else {
                link.style.opacity = '0.7';
                link.style.color = 'var(--accent-red)';
                link.title = 'MAM: Not connected — click to setup';
            }
        } catch(e) {}
    }

    function showMamSetup() {
        const overlay = document.getElementById('mamSetupModal');
        overlay.style.display = 'block';
        const result = document.getElementById('mamResult');
        result.textContent = 'Checking...';
        fetch('/mam-status').then(r => r.json()).then(data => {
            document.getElementById('mamIp').textContent = data.ip || 'unknown';
            if (data.authenticated) {
                document.getElementById('mamAuth').textContent = 'Active';
                document.getElementById('mamAuth').style.color = 'var(--accent-green)';
                result.innerHTML = 'MAM is connected and working. Dynamic seedbox IP is whitelisted.';
            } else {
                document.getElementById('mamAuth').textContent = 'Not connected';
                document.getElementById('mamAuth').style.color = 'var(--accent-red)';
                if (!data.mam_id_configured) {
                    result.innerHTML = '<strong>Setup needed:</strong><br>1. Go to <a href="https://www.myanonamouse.net/preferences/index.php?view=security" target="_blank" style="color:var(--accent-amber)">MAM Security Preferences</a><br>2. Create a new session: ASN-locked (or IP-locked)<br>3. Enable <em>"Allow dynamic seedbox IP"</em><br>4. Copy the <code style="background:var(--bg-input);padding:2px 4px;border-radius:3px;">mam_id</code> value<br>5. Store in Vault: <code style="background:var(--bg-input);padding:2px 4px;border-radius:3px;">vault kv patch secret/servarr mam_id=YOUR_VALUE</code>';
                } else {
                    result.innerHTML = 'mam_id is configured but: <strong>' + esc(data.msg || 'unknown error') + '</strong>';
                }
            }
        });
    }

    async function loadSources() {
        try {
            const r = await fetch('/sources');
            const sources = await r.json();
            const bar = document.getElementById('sourcesBar');
            bar.innerHTML = Object.entries(sources).map(([key, s]) => {
                if (s.type === 'download') return '';
                const active = s.available ? 'active' : '';
                return `<span class="source-chip ${active}"><span class="source-dot"></span>${s.name}</span>`;
            }).join('');
        } catch(e) {}
    }
    loadSources();
    checkMamStatus();
</script>

<div id="mamSetupModal" class="modal-overlay" onclick="if(event.target===this)this.style.display='none'">
    <div class="modal">
        <h2>MAM Setup</h2>
        <div style="font-size:13px;color:var(--text-dim);margin-bottom:16px;">
            Server IP: <strong id="mamIp">...</strong> &middot;
            Status: <strong id="mamAuth">...</strong>
        </div>
        <div id="mamResult" style="font-size:13px;line-height:1.7;margin-bottom:16px;"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('mamSetupModal').style.display='none'">Close</button>
        </div>
    </div>
</div>

</body>
</html>
"""
    return html_content
