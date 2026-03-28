import logging
import os
import re
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

# Anna's Archive changes domains frequently — configurable via env var
ANNAS_DOMAIN = os.getenv("ANNAS_DOMAIN", "annas-archive.gs")
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr.servarr.svc.cluster.local")

# Self-hosted Stacks instance (Anna's Archive download manager)
STACKS_URL = os.getenv("STACKS_URL", "http://annas-archive-stacks.ebooks.svc.cluster.local")


class AnnasArchiveScraper:
    """Anna's Archive ebook search. Prioritizes self-hosted Stacks, falls back to public site."""

    TIMEOUT = 30.0
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self):
        self.base_url = f"https://{ANNAS_DOMAIN}"
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            headers={"User-Agent": self.USER_AGENT},
            follow_redirects=True,
        )
        self._flaresolverr_available = None
        self._stacks_available = None

    async def close(self):
        await self.client.aclose()

    async def _check_stacks(self) -> bool:
        """Check if self-hosted Stacks instance is available."""
        if self._stacks_available is not None:
            return self._stacks_available
        try:
            r = await self.client.get(f"{STACKS_URL}/api/version", timeout=5)
            self._stacks_available = r.status_code == 200
            if self._stacks_available:
                logger.info(f"Stacks available at {STACKS_URL}")
        except Exception:
            self._stacks_available = False
        return self._stacks_available

    async def _check_flaresolverr(self) -> bool:
        """Check if FlareSolverr is available."""
        if self._flaresolverr_available is not None:
            return self._flaresolverr_available
        try:
            r = await self.client.get(f"{FLARESOLVERR_URL}/health", timeout=3)
            self._flaresolverr_available = r.status_code == 200
        except Exception:
            self._flaresolverr_available = False
        if not self._flaresolverr_available:
            logger.warning("FlareSolverr not available — Anna's Archive public search may fail")
        return self._flaresolverr_available

    async def _fetch_via_flaresolverr(self, url: str) -> str | None:
        """Fetch a URL through FlareSolverr to bypass JS challenges."""
        try:
            r = await self.client.post(
                f"{FLARESOLVERR_URL}/v1",
                json={
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": 60000,
                },
                timeout=65.0,
            )
            r.raise_for_status()
            data = r.json()
            solution = data.get("solution", {})
            status = solution.get("status", 0)
            if status == 200:
                return solution.get("response", "")
            logger.error(f"FlareSolverr returned status {status} for {url}")
            return None
        except Exception as e:
            logger.error(f"FlareSolverr request failed: {e}")
            return None

    async def _fetch_public(self, url: str) -> str | None:
        """Fetch from public Anna's Archive, using FlareSolverr if needed."""
        # Try FlareSolverr first
        if await self._check_flaresolverr():
            html = await self._fetch_via_flaresolverr(url)
            if html and "/md5/" in html:
                return html
            if html:
                logger.warning("Anna's Archive results are client-side rendered — FlareSolverr can't extract them")

        # Direct fetch fallback
        try:
            r = await self.client.get(url)
            r.raise_for_status()
            if len(r.text) < 500 and "Verifying" in r.text:
                logger.warning("Anna's Archive returned JS challenge")
                return None
            return r.text
        except Exception as e:
            logger.error(f"Anna's Archive fetch failed: {e}")
            return None

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search Anna's Archive for ebooks. Uses public site (Stacks is download-only)."""
        search_url = f"{self.base_url}/search?q={quote(query)}&content=book_nonfiction&content=book_fiction&ext=epub&ext=pdf&ext=mobi&sort=&lang=en"

        html = await self._fetch_public(search_url)
        if not html:
            return []

        return self._parse_search_results(html)

    def _parse_search_results(self, html: str) -> list[AudiobookResult]:
        """Parse Anna's Archive search results."""
        soup = BeautifulSoup(html, "html.parser")
        results = []

        links = soup.find_all("a", href=re.compile(r"^/md5/"))

        for link in links[:25]:
            try:
                md5_match = re.search(r"/md5/([a-f0-9]+)", link.get("href", ""))
                if not md5_match:
                    continue

                md5 = md5_match.group(1)
                text = link.get_text(separator="\n").strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                if not lines:
                    continue

                title = lines[0] if lines else "Unknown"
                author = None
                format_str = None
                size = None

                full_text = link.get_text()
                for line in lines[1:]:
                    if any(ext in line.lower() for ext in ["epub", "pdf", "mobi", "azw", "djvu", "cbr", "cbz"]):
                        format_str = line.strip()
                    elif re.match(r"[\d.]+\s*(MB|KB|GB|B)", line, re.IGNORECASE):
                        size = line.strip()
                    elif not author and line and not line.startswith("["):
                        author = line.strip()

                meta_match = re.search(r"(epub|pdf|mobi|azw3?|djvu|cbr|cbz)", full_text, re.IGNORECASE)
                if meta_match and not format_str:
                    format_str = meta_match.group(1).upper()

                size_match = re.search(r"([\d.]+\s*(?:MB|KB|GB|B)\b)", full_text, re.IGNORECASE)
                if size_match and not size:
                    size = size_match.group(1)

                cover_url = None
                img = link.find("img")
                if img:
                    cover_url = img.get("src")

                result = AudiobookResult(
                    id=f"annas:{md5}",
                    title=title,
                    author=author,
                    format=format_str,
                    size=size,
                    url=f"{self.base_url}/md5/{md5}",
                    cover_url=cover_url,
                    source="annas",
                    content_type="ebook",
                )
                results.append(result)
            except Exception as e:
                logger.warning(f"Failed to parse Anna's Archive result: {e}")
                continue

        return results

    async def get_detail(self, md5: str) -> AudiobookDetail | None:
        """Get detail page for an Anna's Archive book and extract download links."""
        detail_url = f"{self.base_url}/md5/{md5}"

        html = await self._fetch_public(detail_url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")

        try:
            title_elem = soup.find("div", class_="text-3xl") or soup.find("h1")
            title = title_elem.get_text(strip=True) if title_elem else "Unknown"

            author = None
            author_elem = soup.find("div", class_="italic")
            if author_elem:
                author = author_elem.get_text(strip=True)

            format_str = None
            size = None
            language = None
            description = None

            page_text = soup.get_text()

            format_match = re.search(r"\b(epub|pdf|mobi|azw3?|djvu|cbr|cbz)\b", page_text, re.IGNORECASE)
            if format_match:
                format_str = format_match.group(1).upper()

            size_match = re.search(r"([\d.]+\s*(?:MB|KB|GB))", page_text, re.IGNORECASE)
            if size_match:
                size = size_match.group(1)

            lang_match = re.search(r"Language[:\s]+(\w+)", page_text, re.IGNORECASE)
            if lang_match:
                language = lang_match.group(1)

            desc_elem = soup.find("div", class_="js-md5-top-box-description")
            if desc_elem:
                description = desc_elem.get_text(strip=True)[:500]

            cover_url = None
            img = soup.find("img", src=re.compile(r"covers|book"))
            if img:
                cover_url = img.get("src")

            download_url = None
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href", "")
                if "/fast_download/" in href or "/slow_download/" in href:
                    download_url = href if href.startswith("http") else f"{self.base_url}{href}"
                    break
                if "libgen" in href or "library.lol" in href:
                    download_url = href
                    break

            if not download_url:
                download_url = detail_url

            return AudiobookDetail(
                id=f"annas:{md5}",
                title=title,
                author=author,
                format=format_str,
                size=size,
                url=detail_url,
                cover_url=cover_url,
                magnet_url=download_url,
                description=description,
                language=language,
                source="annas",
                content_type="ebook",
            )
        except Exception as e:
            logger.error(f"Failed to parse Anna's Archive detail: {e}")
            return None

    async def download_file(self, download_url: str) -> tuple[bytes | None, str | None]:
        """Download an ebook file from Anna's Archive mirror.
        Returns (file_bytes, filename) or (None, None) on failure."""
        try:
            response = await self.client.get(download_url, follow_redirects=True)
            response.raise_for_status()

            filename = None
            cd = response.headers.get("content-disposition", "")
            fname_match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
            if fname_match:
                filename = fname_match.group(1).strip()

            if not filename:
                path = response.url.path
                filename = path.split("/")[-1] if "/" in path else "book.epub"

            return response.content, filename
        except Exception as e:
            logger.error(f"Anna's Archive file download failed: {e}")
            return None, None

    async def download_via_stacks(self, md5: str) -> dict:
        """Queue a download via the self-hosted Stacks instance.
        Returns status dict with success/error info."""
        if not await self._check_stacks():
            return {"success": False, "error": "Stacks instance not available"}

        try:
            r = await self.client.post(
                f"{STACKS_URL}/api/queue/add",
                json={"md5": md5, "source": "book-search"},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            data = r.json()
            if r.status_code == 200 and data.get("success"):
                logger.info(f"Queued download via Stacks: {md5}")
                return {"success": True, "message": f"Queued in Stacks — downloading to Calibre Library"}
            else:
                error = data.get("error", f"HTTP {r.status_code}")
                logger.error(f"Stacks queue/add failed: {error}")
                return {"success": False, "error": error}
        except Exception as e:
            logger.error(f"Stacks download request failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_stacks_status(self) -> dict:
        """Get Stacks download queue status."""
        if not await self._check_stacks():
            return {"available": False}
        try:
            r = await self.client.get(f"{STACKS_URL}/api/status", timeout=5)
            if r.status_code == 200:
                return {"available": True, **r.json()}
            return {"available": True, "status": "unknown"}
        except Exception:
            return {"available": True, "status": "error"}
