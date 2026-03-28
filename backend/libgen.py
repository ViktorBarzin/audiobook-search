import logging
import re
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

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

    async def _get_mirror(self) -> str | None:
        """Find a working LibGen mirror."""
        if self._working_mirror:
            try:
                r = await self.client.get(self._working_mirror, timeout=5)
                if r.status_code == 200:
                    return self._working_mirror
            except Exception:
                self._working_mirror = None

        for mirror in LIBGEN_MIRRORS:
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

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search LibGen for ebooks."""
        mirror = await self._get_mirror()
        if not mirror:
            return []

        if self._is_li_mirror(mirror):
            return await self._search_li(query, mirror)
        else:
            return await self._search_classic(query, mirror)

    async def _search_li(self, query: str, mirror: str) -> list[AudiobookResult]:
        """Search libgen.li-style mirrors (index.php with different params)."""
        try:
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
        except Exception as e:
            logger.error(f"LibGen search failed: {e}")
            return []

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
        try:
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
        except Exception as e:
            logger.error(f"LibGen search failed: {e}")
            return []

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
