import logging
import re
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)


class AnnasArchiveScraper:
    """Anna's Archive ebook search scraper."""

    BASE_URL = "https://annas-archive.org"
    TIMEOUT = 20.0
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            headers={"User-Agent": self.USER_AGENT},
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search Anna's Archive for ebooks."""
        search_url = f"{self.BASE_URL}/search?q={quote(query)}&content=book_nonfiction&content=book_fiction&ext=epub&ext=pdf&ext=mobi&sort=&lang=en"

        try:
            response = await self.client.get(search_url)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Anna's Archive search failed: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        # Find result entries - Anna's Archive uses <a> tags with href starting with /md5/
        links = soup.find_all("a", href=re.compile(r"^/md5/"))

        for link in links[:25]:  # Limit to 25 results
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

                # Parse metadata from the text content
                full_text = link.get_text()
                # Author is often after the title
                for line in lines[1:]:
                    if any(ext in line.lower() for ext in ["epub", "pdf", "mobi", "azw", "djvu", "cbr", "cbz"]):
                        format_str = line.strip()
                    elif re.match(r"[\d.]+\s*(MB|KB|GB|B)", line, re.IGNORECASE):
                        size = line.strip()
                    elif not author and line and not line.startswith("["):
                        author = line.strip()

                # Try to extract format and size from combined metadata
                meta_match = re.search(r"(epub|pdf|mobi|azw3?|djvu|cbr|cbz)", full_text, re.IGNORECASE)
                if meta_match and not format_str:
                    format_str = meta_match.group(1).upper()

                size_match = re.search(r"([\d.]+\s*(?:MB|KB|GB|B)\b)", full_text, re.IGNORECASE)
                if size_match and not size:
                    size = size_match.group(1)

                # Extract cover image
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
                    url=f"{self.BASE_URL}/md5/{md5}",
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
        detail_url = f"{self.BASE_URL}/md5/{md5}"

        try:
            response = await self.client.get(detail_url)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Anna's Archive detail fetch failed: {e}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        try:
            # Extract title
            title_elem = soup.find("div", class_="text-3xl") or soup.find("h1")
            title = title_elem.get_text(strip=True) if title_elem else "Unknown"

            # Extract author
            author = None
            author_elem = soup.find("div", class_="italic")
            if author_elem:
                author = author_elem.get_text(strip=True)

            # Extract metadata
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

            # Extract description
            desc_elem = soup.find("div", class_="js-md5-top-box-description")
            if desc_elem:
                description = desc_elem.get_text(strip=True)[:500]

            # Cover image
            cover_url = None
            img = soup.find("img", src=re.compile(r"covers|book"))
            if img:
                cover_url = img.get("src")

            # Find download links - look for fast download partner links
            download_url = None
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href", "")
                link_text = a_tag.get_text(strip=True).lower()
                # Prefer direct download links from library mirrors
                if "/fast_download/" in href or "/slow_download/" in href:
                    download_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
                    break
                if "libgen" in href or "library.lol" in href or "annas-archive.se" in href:
                    download_url = href
                    break

            if not download_url:
                # Use the detail page URL as fallback - user can download from there
                download_url = detail_url

            return AudiobookDetail(
                id=f"annas:{md5}",
                title=title,
                author=author,
                format=format_str,
                size=size,
                url=detail_url,
                cover_url=cover_url,
                magnet_url=download_url,  # Direct download URL for ebooks
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

            # Try to get filename from Content-Disposition header
            filename = None
            cd = response.headers.get("content-disposition", "")
            fname_match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
            if fname_match:
                filename = fname_match.group(1).strip()

            if not filename:
                # Derive from URL
                path = response.url.path
                filename = path.split("/")[-1] if "/" in path else "book.epub"

            return response.content, filename
        except Exception as e:
            logger.error(f"Anna's Archive file download failed: {e}")
            return None, None
