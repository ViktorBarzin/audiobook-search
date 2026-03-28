import logging
import httpx

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)


class OpenLibraryScraper:
    """Open Library search using their REST API. Good metadata and covers."""

    SEARCH_URL = "https://openlibrary.org/search.json"
    TIMEOUT = 10.0

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            headers={"User-Agent": "BookSearch/2.0 (personal library tool)"},
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search Open Library for ebooks."""
        try:
            r = await self.client.get(
                self.SEARCH_URL,
                params={
                    "q": query,
                    "limit": 15,
                    "fields": "key,title,author_name,first_publish_year,cover_i,edition_count,ia,language,number_of_pages_median",
                    "has_fulltext": "true",
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.error(f"Open Library search failed: {e}")
            return []

        results = []
        for doc in data.get("docs", []):
            try:
                title = doc.get("title", "").strip()
                if not title:
                    continue

                work_key = doc.get("key", "")  # e.g. /works/OL123W
                authors = doc.get("author_name", [])
                author = ", ".join(authors) if authors else None
                year = doc.get("first_publish_year")
                cover_id = doc.get("cover_i")
                editions = doc.get("edition_count", 0)
                ia_ids = doc.get("ia", [])  # Internet Archive identifiers
                pages = doc.get("number_of_pages_median")

                cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else None

                format_parts = []
                if year:
                    format_parts.append(str(year))
                if editions:
                    format_parts.append(f"{editions} editions")
                if pages:
                    format_parts.append(f"{pages}p")
                format_str = " · ".join(format_parts) if format_parts else None

                # Use first IA identifier for borrowing/reading
                ia_id = ia_ids[0] if ia_ids else None

                result = AudiobookResult(
                    id=f"openlib:{work_key.split('/')[-1]}",
                    title=title,
                    author=author,
                    format=format_str,
                    size=None,
                    url=f"https://openlibrary.org{work_key}",
                    cover_url=cover_url,
                    source="openlib",
                    content_type="ebook",
                )
                results.append(result)
            except Exception as e:
                logger.warning(f"Failed to parse Open Library result: {e}")
                continue

        return results

    async def get_detail(self, work_id: str) -> AudiobookDetail | None:
        """Get detail for an Open Library work."""
        try:
            r = await self.client.get(
                f"https://openlibrary.org/works/{work_id}.json",
            )
            r.raise_for_status()
            work = r.json()
        except Exception as e:
            logger.error(f"Open Library detail fetch failed: {e}")
            return None

        title = work.get("title", "Unknown")

        # Get author names
        author = None
        author_keys = work.get("authors", [])
        if author_keys:
            author_names = []
            for a in author_keys[:3]:
                a_key = a.get("author", {}).get("key", a.get("key", ""))
                if a_key:
                    try:
                        ar = await self.client.get(f"https://openlibrary.org{a_key}.json")
                        if ar.status_code == 200:
                            author_names.append(ar.json().get("name", ""))
                    except Exception:
                        pass
            author = ", ".join(n for n in author_names if n) or None

        description = None
        desc_raw = work.get("description")
        if isinstance(desc_raw, str):
            description = desc_raw[:500]
        elif isinstance(desc_raw, dict):
            description = desc_raw.get("value", "")[:500]

        covers = work.get("covers", [])
        cover_url = f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg" if covers else None

        # Find a readable/borrowable edition on Internet Archive
        read_url = f"https://openlibrary.org/works/{work_id}"

        # Try to find an edition with IA identifier for direct reading
        try:
            editions_r = await self.client.get(
                f"https://openlibrary.org/works/{work_id}/editions.json",
                params={"limit": 5},
            )
            if editions_r.status_code == 200:
                for entry in editions_r.json().get("entries", []):
                    ia_ids = entry.get("ia_id", []) or entry.get("ocaid", [])
                    if isinstance(ia_ids, str):
                        ia_ids = [ia_ids]
                    if ia_ids:
                        read_url = f"https://archive.org/details/{ia_ids[0]}"
                        break
        except Exception:
            pass

        return AudiobookDetail(
            id=f"openlib:{work_id}",
            title=title,
            author=author,
            format="Open Library",
            size=None,
            url=f"https://openlibrary.org/works/{work_id}",
            cover_url=cover_url,
            magnet_url=read_url,
            description=description,
            language=None,
            source="openlib",
            content_type="ebook",
        )
