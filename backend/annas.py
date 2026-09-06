import logging
import os
import re
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

from backend.goodreads.aa_domains import working_domain
from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

# Anna's Archive changes domains frequently — configurable via env var
ANNAS_DOMAIN = os.getenv("ANNAS_DOMAIN", "annas-archive.gl")
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr.servarr.svc.cluster.local")

# Self-hosted Stacks instance (Anna's Archive download manager)
STACKS_URL = os.getenv("STACKS_URL", "http://annas-archive-stacks.ebooks.svc.cluster.local")

# A year, a language tag, a file size, an ISBN: all of them sit in links next to
# the author on a detail page, and none of them is a person.
_NOT_A_NAME_RE = re.compile(
    r"^(?:\d{4}|[a-z]{2}(?:-[A-Za-z]{2})?|[\d.]+\s?[KMG]B|[\d-]{10,17})$",
    re.IGNORECASE,
)


def _author_link_after(title_elem) -> str | None:
    """The first link after the title whose text reads like a person's name.

    Anna's Archive renders the author as a plain link under the title, with the
    publication year as the next link along. Matching on position rather than on
    a class name survives the site's Tailwind classes being regenerated, which
    is how every other selector here ended up stale.
    """
    # The author is the FIRST link after the title, so a short window is enough
    # and keeps a download link further down the page from being read as a name.
    for link in title_elem.find_all_next("a", limit=6):
        href = link.get("href", "")
        if any(part in href for part in ("/md5/", "_download/", "libgen", "library.lol")):
            continue
        text = link.get_text(strip=True).replace("\xa0", " ").strip()
        if not text or len(text) > 80:
            continue
        # Breadcrumb path segments end in "/", and a bare glyph is a search icon.
        if text.endswith("/") or not re.search(r"[A-Za-z]{2}", text):
            continue
        if _NOT_A_NAME_RE.match(text):
            continue
        return text
    return None


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
        self._stacks_checked_at = 0.0

    async def close(self):
        await self.client.aclose()

    async def _check_stacks(self) -> bool:
        """Check if self-hosted Stacks instance is available. Re-checks every 5 minutes."""
        import time
        now = time.monotonic()
        if self._stacks_available is not None and (now - self._stacks_checked_at) < 300:
            return self._stacks_available
        try:
            r = await self.client.get(f"{STACKS_URL}/api/version", timeout=5)
            self._stacks_available = r.status_code == 200
            if self._stacks_available:
                logger.info(f"Stacks available at {STACKS_URL}")
        except Exception:
            self._stacks_available = False
        self._stacks_checked_at = now
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
        """Fetch from public Anna's Archive. Tries direct first, FlareSolverr as fallback."""
        # Try direct fetch first (works on .gl domain without JS challenge)
        try:
            r = await self.client.get(url)
            r.raise_for_status()
            if len(r.text) < 500 and "Verifying" in r.text:
                logger.warning("Anna's Archive returned JS challenge — trying FlareSolverr")
            elif "/md5/" in r.text or len(r.text) > 5000:
                return r.text
        except Exception as e:
            logger.warning(f"Anna's Archive direct fetch failed: {e}")

        # FlareSolverr fallback for JS-challenged domains
        if await self._check_flaresolverr():
            html = await self._fetch_via_flaresolverr(url)
            if html and "/md5/" in html:
                return html
            if html:
                logger.warning("FlareSolverr couldn't extract results (client-side rendered)")

        return None

    async def _current_base_url(self) -> str:
        """Resolve the base URL, preferring a domain proven to serve results.

        ANNAS_DOMAIN stays authoritative when set explicitly; otherwise the domain
        is discovered from Wikipedia and probed, so a rotation away from whatever
        is hardcoded stops being a silent outage.
        """
        if os.getenv("ANNAS_DOMAIN"):
            return self.base_url
        try:
            domain = await working_domain(self.client)
        except Exception as e:
            logger.warning(f"AA domain discovery failed: {e}")
            return self.base_url
        return f"https://{domain}" if domain else self.base_url

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search Anna's Archive for ebooks. Uses public site (Stacks is download-only)."""
        base_url = await self._current_base_url()
        search_url = f"{base_url}/search?q={quote(query)}&content=book_nonfiction&content=book_fiction&ext=epub&ext=pdf&ext=mobi&sort=&lang=en"

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
        html = await self._fetch_public(f"{self.base_url}/md5/{md5}")
        if not html:
            return None
        return self.parse_detail(html, md5)

    def parse_detail(self, html: str, md5: str) -> AudiobookDetail | None:
        """Read a detail page that somebody else fetched.

        Split from get_detail because Anna's Archive is human-only for us:
        DDoS-Guard 403s /md5/ for plain requests, for six real browser TLS
        handshakes via curl-impersonate, and for the cluster's headful Chrome.
        A phone in a person's hand can load the page, so the iOS Shortcut sends
        what Safari already rendered and the parsing happens here, where it can
        be changed and deployed without anyone editing their shortcut.
        """
        if not html:
            return None

        detail_url = f"{self.base_url}/md5/{md5}"
        soup = BeautifulSoup(html, "html.parser")

        try:
            title_elem = soup.find("div", class_="text-3xl") or soup.find("h1")
            title = title_elem.get_text(strip=True) if title_elem else None
            # Fallback: try og:title or <title> tag (more stable than CSS selectors)
            if not title or title.lower().replace("\u2019", "'") in ("anna's archive", "unknown"):
                og = soup.find("meta", property="og:title")
                if og:
                    title = og.get("content", "").strip()
            if not title or title.lower().replace("\u2019", "'") in ("anna's archive", "unknown"):
                title_tag = soup.find("title")
                if title_tag:
                    t = title_tag.get_text(strip=True)
                    t = re.sub(r"\s*[-–—]\s*Anna['\u2019]?s?\s*Archive\s*$", "", t, flags=re.IGNORECASE)
                    if t:
                        title = t
            if not title:
                title = "Unknown"

            author = None
            author_elem = soup.find("div", class_="italic")
            if author_elem:
                author = author_elem.get_text(strip=True)

            # Fallback 1: Try to extract author from title tag (often "Title by Author - Anna's Archive")
            if not author:
                title_tag = soup.find("title")
                if title_tag:
                    title_text = title_tag.get_text(strip=True)
                    author_match = re.search(r'\bby\s+([^-]+?)(?:\s*[-–—]\s*Anna|$)', title_text, re.IGNORECASE)
                    if author_match:
                        author = author_match.group(1).strip()

            # Fallback 2: Check meta description for "by Author Name" pattern
            if not author:
                og_desc = soup.find("meta", property="og:description")
                if og_desc:
                    desc_text = og_desc.get("content", "")
                    # Match "by FirstName LastName" — require capitalized words to avoid "by famine"
                    author_match = re.search(r'\bby\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', desc_text)
                    if author_match:
                        author = author_match.group(1).strip()

            format_str = None
            size = None
            language = None
            description = None

            page_text = soup.get_text()

            # Fallback 3: Look for "author FirstName LastName" in description text
            if not author:
                desc_elem = soup.find("div", class_="js-md5-top-box-description")
                desc_text = desc_elem.get_text() if desc_elem else page_text[:2000]
                author_match = re.search(r'\bauthor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', desc_text)
                if author_match:
                    author = author_match.group(1).strip()

            # Fallback 4a: the link right under the title. This is what the
            # live page actually uses — read off a real one on 2026-09-06,
            # where the title sits in its own div and the author and the year
            # follow as sibling links, with no div.italic, no "by X" in the
            # <title>, and no og:description. The file-path breadcrumb also
            # names the author, but it comes BEFORE the title in the document
            # and its segments end in "/", so walking forward skips it.
            if not author and title_elem:
                author = _author_link_after(title_elem)

            # Fallback 4: Look for structured "Author: Name" label in page text
            if not author:
                # Require colon after "Author" to avoid matching "Debut author Shen Tao introduces..."
                author_match = re.search(r'\bAuthor\s*:\s*([^\n,;]+)', page_text)
                if author_match:
                    candidate = author_match.group(1).strip()[:80]
                    # Reject if it looks like description text (contains common verbs)
                    if not re.search(r'\b(introduces|presents|brings|writes|explores|takes)\b', candidate, re.IGNORECASE):
                        author = candidate

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

            # Extract ALL download/mirror URLs (for fallback download attempts)
            download_url = None
            mirror_urls = []
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href", "")
                # Primary download link (first match)
                if "/fast_download/" in href or "/slow_download/" in href:
                    full_url = href if href.startswith("http") else f"{self.base_url}{href}"
                    if not download_url:
                        download_url = full_url
                    mirror_urls.append(full_url)
                # Libgen mirrors (direct download capable)
                elif any(mirror in href for mirror in ("libgen.li", "library.lol", "libgen.is", "libgen.rs")):
                    if not download_url:
                        download_url = href
                    mirror_urls.append(href)

            if not download_url:
                download_url = detail_url

            # Add LibGen direct download URLs as fallback
            mirror_urls.append(f"https://libgen.li/get.php?md5={md5}")
            mirror_urls.append(f"https://libgen.is/get.php?md5={md5}")

            if not author:
                # Every author selector here was written against markup that had
                # already moved on, and finding that out cost a night of driving
                # a phone. One log line makes the next move a five-minute fix.
                where = html.find(title) if title else -1
                excerpt = html[max(0, where - 200):where + 600] if where >= 0 else html[:800]
                logger.info(
                    "No author on the page for %s (title %r). Markup around it: %s",
                    md5, title, re.sub(r"\s+", " ", excerpt),
                )

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
                mirror_urls=mirror_urls,
            )
        except Exception as e:
            logger.error(f"Failed to parse Anna's Archive detail: {e}")
            return None

    def _parse_download_response(self, response) -> tuple[bytes | None, str | None]:
        """Parse a download response, extracting filename and validating content."""
        filename = None
        cd = response.headers.get("content-disposition", "")
        fname_match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
        if fname_match:
            filename = fname_match.group(1).strip()

        if not filename:
            path = str(response.url) if hasattr(response, 'url') else ""
            if "/" in path:
                filename = path.split("/")[-1].split("?")[0]

        valid_ext = (".epub", ".pdf", ".mobi", ".azw3", ".djvu", ".cbz", ".cbr", ".fb2")
        if filename and not any(filename.lower().endswith(ext) for ext in valid_ext):
            filename = None

        content = response.content
        check_content = content.lstrip(b'\xef\xbb\xbf')
        if check_content[:1] == b'<' or check_content[:15].lower().startswith(b'<!doctype'):
            return None, None

        return content, filename

    async def download_file(self, download_url: str) -> tuple[bytes | None, str | None]:
        """Download an ebook file. Tries direct HTTP first, then FlareSolverr for CAPTCHA pages.
        Returns (file_bytes, filename) or (None, None) on failure."""
        # Direct download attempt
        try:
            response = await self.client.get(download_url, follow_redirects=True)
            response.raise_for_status()
            content, filename = self._parse_download_response(response)
            if content:
                return content, filename
            ct = response.headers.get("content-type", "")
            logger.warning(f"Download returned HTML instead of ebook file from {download_url} (content-type: {ct}, size: {len(response.content)})")
        except Exception as e:
            logger.warning(f"Direct download failed for {download_url}: {e}")

        # FlareSolverr fallback for CAPTCHA/JS-challenged pages (e.g., AA slow_download)
        is_aa_download = any(p in download_url for p in ("/slow_download/", "/fast_download/", "annas-archive"))
        if is_aa_download and await self._check_flaresolverr():
            logger.info(f"Trying FlareSolverr for download: {download_url}")
            try:
                r = await self.client.post(
                    f"{FLARESOLVERR_URL}/v1",
                    json={
                        "cmd": "request.get",
                        "url": download_url,
                        "maxTimeout": 120000,
                    },
                    timeout=130.0,
                )
                r.raise_for_status()
                data = r.json()
                solution = data.get("solution", {})
                # FlareSolverr returns the final URL after redirects — if it redirected
                # to a file download, the response body might be the file content
                response_text = solution.get("response", "")
                final_url = solution.get("url", "")

                # Check if FlareSolverr followed through to a direct file URL
                if final_url and final_url != download_url:
                    logger.info(f"FlareSolverr redirected to: {final_url}")
                    # Try downloading from the final URL directly
                    try:
                        response = await self.client.get(final_url, follow_redirects=True)
                        response.raise_for_status()
                        content, filename = self._parse_download_response(response)
                        if content:
                            logger.info(f"FlareSolverr redirect download successful: {filename}")
                            return content, filename
                    except Exception as e:
                        logger.warning(f"FlareSolverr redirect download failed: {e}")
            except Exception as e:
                logger.warning(f"FlareSolverr download attempt failed for {download_url}: {e}")

        return None, None

    async def download_from_libgen_md5(self, md5: str) -> tuple[bytes | None, str | None]:
        """Try multiple LibGen endpoints to download by MD5.
        Returns (file_bytes, filename) or (None, None) on failure."""
        # Try direct get.php endpoints first (these return the file directly)
        direct_urls = [
            f"https://libgen.li/get.php?md5={md5}",
            f"https://libgen.is/get.php?md5={md5}",
        ]
        for url in direct_urls:
            try:
                logger.info(f"Trying LibGen direct download: {url}")
                result = await self.download_file(url)
                if result[0]:
                    return result
            except Exception as e:
                logger.warning(f"LibGen direct download failed from {url}: {e}")

        # Try library.lol (requires parsing the page for the actual download link)
        for path in ("main", "fiction"):
            try:
                lol_url = f"https://library.lol/{path}/{md5}"
                logger.info(f"Trying library.lol: {lol_url}")
                # library.lol sometimes has self-signed certs
                client = httpx.AsyncClient(timeout=15, follow_redirects=True, verify=False)
                try:
                    r = await client.get(lol_url)
                    if r.status_code == 200 and len(r.text) > 500:
                        soup = BeautifulSoup(r.text, "html.parser")
                        for a_tag in soup.find_all("a", href=True):
                            href = a_tag.get("href", "")
                            text = a_tag.get_text(strip=True).lower()
                            if "get" in text or "cloudflare" in text:
                                logger.info(f"Found download link on library.lol: {href}")
                                result = await self.download_file(href)
                                if result[0]:
                                    return result
                finally:
                    await client.aclose()
            except Exception as e:
                logger.warning(f"library.lol download failed for {path}/{md5}: {e}")

        logger.warning(f"All LibGen download methods failed for {md5}")
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
            elif r.status_code == 200 and "already downloaded" in data.get("message", "").lower():
                logger.info(f"Stacks: already downloaded {md5}")
                return {"success": True, "message": "Already downloaded via Stacks"}
            else:
                error = data.get("error", f"HTTP {r.status_code}")
                logger.error(f"Stacks queue/add failed: {error}")
                return {"success": False, "error": error}
        except Exception as e:
            logger.error(f"Stacks download request failed: {e}")
            return {"success": False, "error": str(e)}

    async def stacks_force_redownload(self, md5: str) -> dict:
        """Delete a completed entry from Stacks history and re-queue it.
        Used when Stacks says 'already downloaded' but the file is missing."""
        if not await self._check_stacks():
            return {"success": False, "error": "Stacks instance not available"}

        try:
            # Get the download ID from status
            r = await self.client.get(f"{STACKS_URL}/api/status", timeout=5)
            if r.status_code != 200:
                return {"success": False, "error": "Failed to get Stacks status"}

            status = r.json()
            download_id = None
            for item in status.get("recent_history", []):
                if item.get("md5") == md5:
                    download_id = item.get("id")
                    break

            if download_id is None:
                # Not in history — just try to queue normally
                return await self.download_via_stacks(md5)

            # Stacks has no delete API — delete directly from SQLite
            import sqlite3
            db_path = os.getenv("STACKS_DB_PATH", "/stacks-config/queue.db")
            try:
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute("DELETE FROM downloads WHERE md5 = ?", (md5,))
                deleted = c.rowcount
                conn.commit()
                conn.close()
                logger.info(f"Stacks: deleted {deleted} history entry for {md5} (id={download_id})")
            except Exception as e:
                logger.error(f"Stacks: failed to delete history entry for {md5}: {e}")
                return {"success": False, "error": f"DB delete failed: {e}"}

            # Re-queue
            return await self.download_via_stacks(md5)
        except Exception as e:
            logger.error(f"Stacks force redownload failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_stacks_status(self) -> dict:
        """Get Stacks download queue status. Detects DB corruption."""
        if not await self._check_stacks():
            return {"available": False}
        try:
            r = await self.client.get(f"{STACKS_URL}/api/status", timeout=5)
            if r.status_code == 200:
                return {"available": True, **r.json()}
            # 500 errors often indicate DB corruption
            body = r.text
            if "malformed" in body or "database disk image" in body:
                return {"available": True, "error": "database corrupted", "status": "error"}
            return {"available": True, "status": "unknown"}
        except Exception as e:
            return {"available": True, "status": "error", "error": str(e)}
