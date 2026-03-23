import re
from urllib.parse import quote, urljoin
import httpx
from bs4 import BeautifulSoup

from backend.models import AudiobookResult, AudiobookDetail


class AudioBookBayScraper:
    BASE_URL = "https://audiobookbay.lu"
    TIMEOUT = 15.0
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
        """Search AudioBookBay for audiobooks."""
        search_url = f"{self.BASE_URL}/?s={quote(query)}&tt=1"

        try:
            response = await self.client.get(search_url)
            response.raise_for_status()
        except Exception as e:
            print(f"Search request failed: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        # Find all post elements
        posts = soup.find_all("div", class_="post")

        for post in posts:
            try:
                # Extract title and URL
                title_elem = post.find("h2", class_="postTitle")
                if not title_elem:
                    title_elem = post.find(class_="postTitle")

                if not title_elem:
                    continue

                link_elem = title_elem.find("a")
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                url = link_elem.get("href", "")

                # Use URL path as ID (strip leading/trailing slashes)
                book_id = url.strip("/") if url else ""
                if not book_id:
                    continue

                # Extract cover image
                cover_url = None
                post_content = post.find(class_="postContent")
                if post_content:
                    img = post_content.find("img")
                    if img:
                        cover_url = img.get("src")

                # Extract metadata from post content text
                author = None
                narrator = None
                format_type = None
                size = None

                if post_content:
                    content_text = post_content.get_text()

                    author_match = re.search(r"(?:Written by|Author)[:\s]+(.+?)(?:\s*(?:Format|Narrator|Read by|Size|Bitrate)\b|\n|$)", content_text, re.IGNORECASE)
                    if author_match:
                        author = author_match.group(1).strip().rstrip(",")

                    narrator_match = re.search(r"(?:Read by|Narrated by|Narrator)[:\s]+(.+?)(?:\s*(?:Format|Written by|Author|Size|Bitrate)\b|\n|$)", content_text, re.IGNORECASE)
                    if narrator_match:
                        narrator = narrator_match.group(1).strip().rstrip(",")

                    format_match = re.search(r"Format[:\s]+(.+?)(?:\s*(?:Size|File size|Narrator|Written by|Author)\b|\n|$)", content_text, re.IGNORECASE)
                    if format_match:
                        format_type = format_match.group(1).strip()

                    size_match = re.search(r"(?:Size|File size)[:\s]+([^\n\r]+)", content_text, re.IGNORECASE)
                    if size_match:
                        size = size_match.group(1).strip()

                result = AudiobookResult(
                    id=book_id,
                    title=title,
                    author=author,
                    narrator=narrator,
                    format=format_type,
                    size=size,
                    url=url,
                    cover_url=cover_url,
                )
                results.append(result)

            except Exception as e:
                print(f"Failed to parse post: {e}")
                continue

        return results

    async def get_detail(self, book_path: str) -> AudiobookDetail | None:
        """Get detailed information for a specific audiobook."""
        # Construct full URL
        if book_path.startswith("http"):
            detail_url = book_path
        else:
            # Ensure path has trailing slash (AudioBookBay requires it)
            clean_path = book_path.strip("/")
            detail_url = f"{self.BASE_URL}/{clean_path}/"

        try:
            response = await self.client.get(detail_url)
            response.raise_for_status()
        except Exception as e:
            print(f"Detail request failed: {e}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        try:
            # Extract basic info (same as search)
            title_elem = soup.find("h1", class_="postTitle")
            if not title_elem:
                title_elem = soup.find(class_="postTitle")

            title = title_elem.get_text(strip=True) if title_elem else "Unknown"

            # Extract cover
            cover_url = None
            post_content = soup.find(class_="postContent")
            if post_content:
                img = post_content.find("img")
                if img:
                    cover_url = img.get("src")

            # Extract metadata
            author = None
            narrator = None
            format_type = None
            size = None
            description = None
            language = None

            page_text = soup.get_text()

            # Extract info hash for magnet link
            info_hash = None
            info_hash_match = re.search(r"Info Hash[:\s]+([A-Fa-f0-9]{40})", page_text, re.IGNORECASE)
            if info_hash_match:
                info_hash = info_hash_match.group(1).strip()

            # Also try to find existing magnet link
            magnet_link = None
            magnet_elem = soup.find("a", href=re.compile(r"^magnet:\?"))
            if magnet_elem:
                magnet_link = magnet_elem.get("href")

            # If we have info hash but no magnet link, construct one
            if info_hash and not magnet_link:
                encoded_title = quote(title)
                magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={encoded_title}"

                # Try to find tracker URLs
                trackers = re.findall(r"(?:udp|http)://[^\s<>\"']+", page_text)
                for tracker in trackers[:3]:  # Add first 3 trackers
                    magnet_link += f"&tr={quote(tracker)}"

            if not magnet_link:
                print(f"No magnet link found for {title}")
                return None

            # Extract metadata from content
            if post_content:
                content_text = post_content.get_text()

                author_match = re.search(r"(?:Written by|Author)[:\s]+(.+?)(?:\s*(?:Format|Narrator|Read by|Size|Bitrate)\b|\n|$)", content_text, re.IGNORECASE)
                if author_match:
                    author = author_match.group(1).strip().rstrip(",")

                narrator_match = re.search(r"(?:Read by|Narrated by|Narrator)[:\s]+(.+?)(?:\s*(?:Format|Written by|Author|Size|Bitrate)\b|\n|$)", content_text, re.IGNORECASE)
                if narrator_match:
                    narrator = narrator_match.group(1).strip().rstrip(",")

                format_match = re.search(r"Format[:\s]+(.+?)(?:\s*(?:Size|File size|Narrator|Written by|Author)\b|\n|$)", content_text, re.IGNORECASE)
                if format_match:
                    format_type = format_match.group(1).strip()

                size_match = re.search(r"(?:Size|File size)[:\s]+([^\n\r]+)", content_text, re.IGNORECASE)
                if size_match:
                    size = size_match.group(1).strip()

                language_match = re.search(r"Language[:\s]+(.+?)(?:\s*(?:Format|Narrator|Written by|Author|Size)\b|\n|$)", content_text, re.IGNORECASE)
                if language_match:
                    language = language_match.group(1).strip()

                # Try to extract description (usually in a paragraph)
                paragraphs = post_content.find_all("p")
                if paragraphs:
                    # Take the longest paragraph as description
                    descriptions = [p.get_text(strip=True) for p in paragraphs]
                    descriptions = [d for d in descriptions if len(d) > 100]
                    if descriptions:
                        description = max(descriptions, key=len)

            detail = AudiobookDetail(
                id=book_path.rstrip("/").split("/")[-1],
                title=title,
                author=author,
                narrator=narrator,
                format=format_type,
                size=size,
                url=detail_url,
                cover_url=cover_url,
                magnet_url=magnet_link,
                description=description,
                language=language,
            )

            return detail

        except Exception as e:
            print(f"Failed to parse detail page: {e}")
            return None
