import asyncio
import logging
import re
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

from backend.goodreads.isbn import to_isbn13
from backend.goodreads.matcher import Candidate
from backend.goodreads.sources import SourceUnavailable, rows_to_candidates
from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

# LibGen mirrors — tried in order. .li and .vg use different URL/HTML format.
LIBGEN_MIRRORS = [
    "https://libgen.li",
    "https://libgen.vg",
    "https://libgen.is",
    "https://libgen.rs",
    "https://libgen.st",
]


# A dropped connection to a healthy mirror is common enough to matter: measured
# ~12% of searches on 2026-08-16 while libgen.li itself answered in under 3s.
# Without a retry that blip returned zero results and read, from the outside,
# exactly like an IP block.
SEARCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5


class LibGenScraper:
    """Library Genesis ebook search. Direct search without JS challenges."""

    TIMEOUT = 15.0
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            headers={"User-Agent": self.USER_AGENT},
            follow_redirects=True,
        )
        self._working_mirror = None

    async def close(self):
        await self.client.aclose()

    async def _get_mirror(self, exclude: set[str] | None = None) -> str | None:
        """Find a working LibGen mirror, skipping any in `exclude`.

        Health checks stay on a short timeout, which is what keeps fall-through
        cheap: the dead mirrors (.is/.rs/.st) cost 5s each here rather than the
        20s connect timeout a real search against them would burn.
        """
        exclude = exclude or set()
        if self._working_mirror and self._working_mirror not in exclude:
            try:
                r = await self.client.get(self._working_mirror, timeout=5)
                if r.status_code == 200:
                    return self._working_mirror
            except Exception:
                self._working_mirror = None

        for mirror in LIBGEN_MIRRORS:
            if mirror in exclude:
                continue
            try:
                r = await self.client.get(mirror, timeout=5)
                if r.status_code == 200:
                    self._working_mirror = mirror
                    logger.info(f"LibGen mirror: {mirror}")
                    return mirror
            except Exception:
                continue
        logger.error("No LibGen mirrors available")
        return None

    def _is_li_mirror(self, mirror: str) -> bool:
        """Check if this is a libgen.li-style mirror (different URL/HTML format)."""
        return "libgen.li" in mirror or "libgen.vg" in mirror

    # ------------------------------------------------------------------ #
    # Direct download (ads.php -> get.php)                                #
    # ------------------------------------------------------------------ #
    # Anna's Archive is no longer usable as a *fetch* route: /slow_download/
    # serves a challenge FlareSolverr times out on, and /fast_download/ is paid.
    # libgen.li still serves the file free, but only via a two-step flow — the
    # ads.php landing page carries a single-use keyed get.php link that must be
    # fetched with ads.php as the Referer. A plain GET of ads.php yields HTML.

    GET_LINK_RE = re.compile(r'href=["\'](get\.php\?[^"\']*key=[^"\']+)["\']', re.I)
    FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.I)

    @staticmethod
    def _extract_get_link(html: str) -> str | None:
        """Pull the keyed `get.php?md5=..&key=..` link out of an ads.php page."""
        m = LibGenScraper.GET_LINK_RE.search(html or "")
        return m.group(1) if m else None

    @staticmethod
    def _filename_from_disposition(header: str | None) -> str | None:
        """Read the filename out of a Content-Disposition header.

        libgen.li emits a leading space inside the quotes, so strip aggressively.
        """
        if not header:
            return None
        m = LibGenScraper.FILENAME_RE.search(header)
        if not m:
            return None
        return m.group(1).strip().strip('"').strip() or None

    @staticmethod
    def _looks_like_ebook(data: bytes | None) -> bool:
        """Reject challenge/error pages served with a 200.

        Magic-byte matching is not enough: a MOBI begins with the PalmDB name
        field (arbitrary text), so only the negative check is reliable.
        """
        if not data or len(data) < 1024:
            return False
        head = data[:64].lstrip().lower()
        return not (head.startswith(b"<!doctype") or head.startswith(b"<html"))

    async def download_file(self, md5: str) -> tuple[bytes | None, str | None]:
        """Fetch an ebook by md5 from libgen. Returns (bytes, filename)."""
        mirror = self._working_mirror or await self._get_mirror()
        if not mirror or not self._is_li_mirror(mirror):
            mirror = "https://libgen.li"

        ads_url = f"{mirror}/ads.php?md5={md5}"
        try:
            r = await self.client.get(ads_url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"LibGen ads page failed for {md5}: {e}")
            return None, None

        link = self._extract_get_link(r.text)
        if not link:
            logger.warning(f"LibGen: no keyed download link on ads page for {md5}")
            return None, None

        dl_url = f"{mirror}/{link.lstrip('/')}"
        try:
            # Referer is load-bearing — libgen.li rejects the keyed URL without it.
            resp = await self.client.get(dl_url, headers={"Referer": ads_url}, timeout=120)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"LibGen download failed for {md5}: {e}")
            return None, None

        data = resp.content
        if not self._looks_like_ebook(data):
            logger.warning(
                f"LibGen returned {len(data)} bytes of non-ebook content for {md5}"
            )
            return None, None

        filename = self._filename_from_disposition(
            resp.headers.get("content-disposition")
        ) or f"{md5}.epub"
        logger.info(f"LibGen direct download OK: {filename} ({len(data)} bytes)")
        return data, filename

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search LibGen for ebooks, retrying transient connection failures.

        A mirror that answers with an EMPTY result table has answered — that is
        "no such book", and it returns immediately. Only a raised transport
        error is retried, which is why the per-mirror helpers propagate instead
        of swallowing exceptions.
        """
        mirror = await self._get_mirror()
        if not mirror:
            return []

        tried: set[str] = set()
        while mirror:
            tried.add(mirror)
            for attempt in range(1, SEARCH_ATTEMPTS + 1):
                try:
                    if self._is_li_mirror(mirror):
                        return await self._search_li(query, mirror)
                    return await self._search_classic(query, mirror)
                except Exception as e:
                    # str(e) is empty for the httpx connection errors this
                    # actually hits, so the TYPE is the only usable detail.
                    logger.warning(
                        "LibGen search failed on %s (attempt %d/%d): %s: %s",
                        mirror, attempt, SEARCH_ATTEMPTS, type(e).__name__, e,
                    )
                    if attempt < SEARCH_ATTEMPTS:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
            # This mirror is exhausted; look for another healthy one.
            self._working_mirror = None
            mirror = await self._get_mirror(exclude=tried)
            if mirror:
                logger.info("LibGen falling through to %s", mirror)

        logger.error("LibGen search failed on every mirror tried: %s", ", ".join(sorted(tried)))
        return []

    async def search_by_isbn(self, isbn: str | None) -> list[Candidate]:
        """Look a book up by identifier, returning downloadable files.

        libgen indexes ISBN-13 only: querying the ISBN-10 that Goodreads supplies
        returns an empty result set rather than an error, so the conversion is
        required for this path to find anything.
        """
        isbn13 = to_isbn13(isbn)
        if not isbn13:
            return []

        html = await self._get_with_retry(
            {"req": isbn13, "objects[]": "f", "res": "25"},
            what=f"ISBN {isbn13}",
        )
        return rows_to_candidates(html)

    async def search_candidates(self, query: str) -> list[Candidate]:
        """Search by free text, returning Candidates rather than UI results.

        Kept separate from search() so the interactive UI's result shape stays
        untouched while the pipeline gets the language and byte-size fields it
        needs to judge a match.
        """
        html = await self._get_with_retry(
            {
                "req": query,
                "columns[]": ["t", "a"],
                "objects[]": "f",
                "topics[]": "l",
                "res": "25",
            },
            what=repr(query),
        )
        return rows_to_candidates(html)

    # Searches for the Goodreads pipeline are separate from the interactive
    # search path above, which retries and then returns [] on failure. The
    # pipeline attempts each book exactly once, so "" and "not found" must not
    # look alike: this path raises SourceUnavailable instead of returning empty,
    # letting the poller defer the book rather than spend its single attempt.
    PIPELINE_SEARCH_TIMEOUT = 30.0
    PIPELINE_SEARCH_ATTEMPTS = 2

    async def _get_with_retry(self, params: dict, what: str) -> str:
        mirror = self._working_mirror or await self._get_mirror()
        if not mirror or not self._is_li_mirror(mirror):
            raise SourceUnavailable("no libgen.li-style mirror available")

        last_error: Exception | None = None
        for attempt in range(self.PIPELINE_SEARCH_ATTEMPTS):
            try:
                r = await self.client.get(
                    f"{mirror}/index.php", params=params,
                    timeout=self.PIPELINE_SEARCH_TIMEOUT,
                )
                r.raise_for_status()
                return r.text
            except Exception as e:
                last_error = e
                logger.warning(
                    "LibGen search for %s failed (attempt %d/%d): %s",
                    what, attempt + 1, self.PIPELINE_SEARCH_ATTEMPTS,
                    e or type(e).__name__,
                )
                if attempt + 1 < self.PIPELINE_SEARCH_ATTEMPTS:
                    await asyncio.sleep(2)

        raise SourceUnavailable(f"libgen unreachable for {what}: {last_error}")

    async def _search_li(self, query: str, mirror: str) -> list[AudiobookResult]:
        """Search libgen.li-style mirrors (index.php with different params)."""
        # Transport errors PROPAGATE: `search` owns the retry and the
        # fall-through, and it can only distinguish "request failed" from
        # "no matches" if this raises rather than returning [].
        r = await self.client.get(
            f"{mirror}/index.php",
            params={
                "req": query,
                "columns[]": ["t", "a"],
                "objects[]": "f",
                "topics[]": "l",
                "res": "25",
            },
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", class_="table-striped")
        if not table:
            return []

        results = []
        rows = table.find_all("tr")[1:]  # Skip header

        for row in rows[:25]:
            try:
                cols = row.find_all("td")
                if len(cols) < 9:
                    continue

                title = cols[0].get_text(strip=True)
                author = cols[1].get_text(strip=True) or None
                year = cols[3].get_text(strip=True)
                size = cols[6].get_text(strip=True)
                ext = cols[7].get_text(strip=True).upper()

                # Extract MD5 from links in last column
                md5 = None
                for link in cols[8].find_all("a", href=True):
                    md5_match = re.search(r"md5=([a-fA-F0-9]{32})", link.get("href", ""), re.IGNORECASE)
                    if md5_match:
                        md5 = md5_match.group(1).lower()
                        break

                if not md5 or not title:
                    continue

                format_str = f"{ext} ({year})" if year else ext

                results.append(AudiobookResult(
                    id=f"libgen:{md5}",
                    title=title,
                    author=author,
                    format=format_str,
                    size=size,
                    url=f"{mirror}/ads.php?md5={md5}",
                    cover_url=None,
                    source="libgen",
                    content_type="ebook",
                ))
            except Exception as e:
                logger.warning(f"Failed to parse LibGen result: {e}")
                continue

        return results

    async def _search_classic(self, query: str, mirror: str) -> list[AudiobookResult]:
        """Search classic libgen.is-style mirrors."""
        # Propagates for the same reason as _search_li — see there.
        r = await self.client.get(
            f"{mirror}/search.php",
            params={
                "req": query,
                "lg_topic": "libgen",
                "open": "0",
                "view": "simple",
                "res": "25",
                "phrase": "1",
                "column": "def",
            },
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", class_="c")
        if not table:
            return []

        results = []
        rows = table.find_all("tr")[1:]

        for row in rows[:25]:
            try:
                cols = row.find_all("td")
                if len(cols) < 10:
                    continue

                author = cols[1].get_text(strip=True) or None
                title = cols[2].get_text(strip=True)
                year = cols[4].get_text(strip=True) if len(cols) > 4 else None
                size = cols[7].get_text(strip=True) if len(cols) > 7 else None
                ext = cols[8].get_text(strip=True).upper() if len(cols) > 8 else None

                md5 = None
                for link in row.find_all("a", href=True):
                    md5_match = re.search(r"md5=([a-fA-F0-9]{32})", link.get("href", ""), re.IGNORECASE)
                    if md5_match:
                        md5 = md5_match.group(1).lower()
                        break

                if not md5 or not title:
                    continue

                format_str = f"{ext} ({year})" if ext and year else (ext or year)

                results.append(AudiobookResult(
                    id=f"libgen:{md5}",
                    title=title,
                    author=author,
                    format=format_str,
                    size=size,
                    url=f"{mirror}/book/index.php?md5={md5}",
                    cover_url=None,
                    source="libgen",
                    content_type="ebook",
                ))
            except Exception as e:
                logger.warning(f"Failed to parse LibGen result: {e}")
                continue

        return results

    async def get_detail(self, md5: str) -> AudiobookDetail | None:
        """Get download link for a LibGen book by MD5."""
        mirror = await self._get_mirror()
        if not mirror:
            return None

        # Try the JSON API for metadata (only works on classic mirrors)
        title = "Unknown"
        author = None
        ext = "epub"
        size = None
        language = None
        description = None
        cover = None

        if not self._is_li_mirror(mirror):
            try:
                r = await self.client.get(
                    f"{mirror}/json.php",
                    params={"ids": md5, "fields": "Title,Author,Extension,Filesize,Language,Descr,MD5,coverurl"},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        item = data[0]
                        title = item.get("Title", title)
                        author = item.get("Author") or None
                        ext = item.get("Extension", ext)
                        language = item.get("Language") or None
                        description = item.get("Descr") or None
                        cover = item.get("coverurl") or None
                        size_bytes = item.get("Filesize")
                        if size_bytes:
                            try:
                                sb = int(size_bytes)
                                if sb > 1_073_741_824:
                                    size = f"{sb / 1_073_741_824:.2f} GB"
                                elif sb > 1_048_576:
                                    size = f"{sb / 1_048_576:.1f} MB"
                                else:
                                    size = f"{sb / 1024:.0f} KB"
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass

        if cover and not cover.startswith("http"):
            cover = f"{mirror}/covers/{cover}"

        # Download URL — use Anna's Archive for reliable downloads
        download_url = f"https://library.lol/main/{md5}"

        return AudiobookDetail(
            id=f"libgen:{md5}",
            title=title,
            author=author,
            format=ext.upper() if ext else None,
            size=size,
            url=f"{mirror}/ads.php?md5={md5}" if self._is_li_mirror(mirror) else f"{mirror}/book/index.php?md5={md5}",
            cover_url=cover,
            magnet_url=download_url,
            description=description[:500] if description else None,
            language=language,
            source="libgen",
            content_type="ebook",
        )
