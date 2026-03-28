import logging
import re
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

# LibGen mirrors — tried in order
LIBGEN_MIRRORS = [
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

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search LibGen for ebooks."""
        mirror = await self._get_mirror()
        if not mirror:
            return []

        search_url = f"{mirror}/search.php"
        params = {
            "req": query,
            "lg_topic": "libgen",
            "open": "0",
            "view": "simple",
            "res": "25",
            "phrase": "1",
            "column": "def",
        }

        try:
            r = await self.client.get(search_url, params=params)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"LibGen search failed: {e}")
            return []

        return self._parse_search_results(r.text, mirror)

    def _parse_search_results(self, html: str, mirror: str) -> list[AudiobookResult]:
        """Parse LibGen search results page."""
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # LibGen uses a table with class 'c' for results
        table = soup.find("table", class_="c")
        if not table:
            return []

        rows = table.find_all("tr")[1:]  # Skip header row

        for row in rows[:25]:
            try:
                cols = row.find_all("td")
                if len(cols) < 10:
                    continue

                # Column layout: ID, Author, Title, Publisher, Year, Pages, Language, Size, Extension, Mirror links
                author = cols[1].get_text(strip=True) or None
                title_cell = cols[2]
                title_link = title_cell.find("a", href=re.compile(r"book/index\.php|/fiction/"))
                title = title_link.get_text(strip=True) if title_link else cols[2].get_text(strip=True)

                if not title:
                    continue

                # Extract ID from the link
                book_id = None
                if title_link:
                    href = title_link.get("href", "")
                    id_match = re.search(r"id=(\d+)", href)
                    if id_match:
                        book_id = id_match.group(1)

                # Extract MD5 from mirror links
                md5 = None
                for link in cols[9].find_all("a", href=True) if len(cols) > 9 else []:
                    md5_match = re.search(r"md5=([a-fA-F0-9]{32})", link.get("href", ""))
                    if md5_match:
                        md5 = md5_match.group(1).upper()
                        break

                if not md5 and not book_id:
                    # Try all links in the row
                    for link in row.find_all("a", href=True):
                        md5_match = re.search(r"md5=([a-fA-F0-9]{32})", link.get("href", ""), re.IGNORECASE)
                        if md5_match:
                            md5 = md5_match.group(1).upper()
                            break

                year = cols[4].get_text(strip=True) if len(cols) > 4 else None
                size = cols[7].get_text(strip=True) if len(cols) > 7 else None
                ext = cols[8].get_text(strip=True).upper() if len(cols) > 8 else None

                format_str = ext
                if year:
                    format_str = f"{ext} ({year})" if ext else year

                result_id = f"libgen:{md5}" if md5 else f"libgen:{book_id or title[:20]}"

                result = AudiobookResult(
                    id=result_id,
                    title=title,
                    author=author if author and author != "" else None,
                    format=format_str,
                    size=size,
                    url=f"{mirror}/book/index.php?md5={md5}" if md5 else f"{mirror}/search.php?req={quote(title)}",
                    cover_url=None,
                    source="libgen",
                    content_type="ebook",
                )
                results.append(result)
            except Exception as e:
                logger.warning(f"Failed to parse LibGen result: {e}")
                continue

        return results

    async def get_detail(self, md5: str) -> AudiobookDetail | None:
        """Get download link for a LibGen book by MD5."""
        mirror = await self._get_mirror()
        if not mirror:
            return None

        # Use the JSON API for metadata
        try:
            json_url = f"{mirror}/json.php"
            r = await self.client.get(json_url, params={"ids": md5, "fields": "Title,Author,Extension,Filesize,Language,Descr,MD5,coverurl"})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                item = data[0]
            else:
                item = {}
        except Exception:
            item = {}

        title = item.get("Title", item.get("title", "Unknown"))
        author = item.get("Author", item.get("author")) or None
        ext = item.get("Extension", item.get("extension", "epub"))
        size_bytes = item.get("Filesize", item.get("filesize"))
        language = item.get("Language", item.get("language")) or None
        description = item.get("Descr", item.get("descr")) or None
        cover = item.get("coverurl") or None

        size = None
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

        if cover and not cover.startswith("http"):
            cover = f"{mirror}/covers/{cover}"

        # Get download URL from library.lol
        download_url = f"https://library.lol/main/{md5}"

        return AudiobookDetail(
            id=f"libgen:{md5}",
            title=title,
            author=author,
            format=ext.upper() if ext else None,
            size=size,
            url=f"{mirror}/book/index.php?md5={md5}",
            cover_url=cover,
            magnet_url=download_url,
            description=description[:500] if description else None,
            language=language,
            source="libgen",
            content_type="ebook",
        )
