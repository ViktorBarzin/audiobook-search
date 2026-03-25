import json
import logging
from urllib.parse import quote
import httpx

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)


class MAMScraper:
    """MyAnonamouse private tracker search."""

    BASE_URL = "https://www.myanonamouse.net"
    SEARCH_URL = f"{BASE_URL}/tor/js/loadSearchJSONbasic.php"
    TIMEOUT = 15.0

    # Audiobook category IDs on MAM
    AUDIOBOOK_CATS = [13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139]

    def __init__(self, mam_id: str):
        self.mam_id = mam_id
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            cookies={"mam_id": mam_id},
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search MAM for audiobooks."""
        params = {
            "tor[text]": query,
            "tor[searchType]": "all",
            "tor[searchIn]": "torrents",
            "tor[browseFlagsHideVs498]": "0",
            "tor[startNumber]": "0",
            "tor[sortType]": "default",
            "perpage": "25",
        }

        try:
            response = await self.client.get(self.SEARCH_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"MAM search failed: {e}")
            return []

        if data.get("status") != "Success" or not data.get("data"):
            return []

        results = []
        for item in data["data"]:
            try:
                torrent_id = str(item.get("id", ""))
                title = item.get("title", "").strip()
                if not torrent_id or not title:
                    continue

                # Parse author from JSON string
                author = None
                author_info = item.get("author_info")
                if author_info:
                    try:
                        authors = json.loads(author_info) if isinstance(author_info, str) else author_info
                        if isinstance(authors, dict):
                            author = ", ".join(authors.values())
                    except (json.JSONDecodeError, TypeError):
                        pass

                narrator = item.get("narrator", None)
                if narrator:
                    narrator = narrator.strip()

                # Size in bytes
                size_bytes = item.get("size")
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

                filetype = item.get("filetype", "")
                category = item.get("cat_name", "")
                format_str = f"{filetype}" if filetype else None
                if category and format_str:
                    format_str = f"{category} / {format_str}"
                elif category:
                    format_str = category

                seeders = item.get("seeders", 0)
                leechers = item.get("leechers", 0)
                if format_str:
                    format_str += f" | S:{seeders} L:{leechers}"

                result = AudiobookResult(
                    id=f"mam:{torrent_id}",
                    title=title,
                    author=author,
                    narrator=narrator,
                    format=format_str,
                    size=size,
                    url=f"{self.BASE_URL}/t/{torrent_id}",
                    cover_url=None,
                    source="mam",
                )
                results.append(result)
            except Exception as e:
                logger.warning(f"Failed to parse MAM result: {e}")
                continue

        return results

    async def get_detail(self, torrent_id: str) -> AudiobookDetail | None:
        """Get detail for a MAM torrent. The torrent_id should be the numeric ID."""
        # Use the JSON endpoint to get torrent info
        params = {
            "tor[text]": "",
            "tor[searchType]": "all",
            "tor[searchIn]": "torrents",
            "tor[id]": torrent_id,
        }

        try:
            response = await self.client.get(self.SEARCH_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"MAM detail fetch failed: {e}")
            return None

        if data.get("status") != "Success" or not data.get("data"):
            return None

        item = data["data"][0]
        title = item.get("title", "Unknown").strip()

        author = None
        author_info = item.get("author_info")
        if author_info:
            try:
                authors = json.loads(author_info) if isinstance(author_info, str) else author_info
                if isinstance(authors, dict):
                    author = ", ".join(authors.values())
            except (json.JSONDecodeError, TypeError):
                pass

        narrator = item.get("narrator")
        if narrator:
            narrator = narrator.strip()

        filetype = item.get("filetype", "")
        category = item.get("cat_name", "")
        lang_list = item.get("language")
        language = None
        if lang_list:
            if isinstance(lang_list, list):
                language = ", ".join(str(l) for l in lang_list)
            elif isinstance(lang_list, str):
                language = lang_list

        size_bytes = item.get("size")
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

        description = item.get("description", None)

        # Torrent download URL (will be downloaded server-side and sent to qBittorrent)
        torrent_url = f"{self.BASE_URL}/tor/download.php/{torrent_id}"

        return AudiobookDetail(
            id=f"mam:{torrent_id}",
            title=title,
            author=author,
            narrator=narrator,
            format=f"{category} / {filetype}" if filetype else category,
            size=size,
            url=f"{self.BASE_URL}/t/{torrent_id}",
            cover_url=None,
            magnet_url=torrent_url,  # This is a torrent URL, handled in download endpoint
            description=description,
            language=language,
            source="mam",
        )

    async def download_torrent_file(self, torrent_id: str) -> bytes | None:
        """Download the .torrent file from MAM."""
        url = f"{self.BASE_URL}/tor/download.php/{torrent_id}"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            if b"d8:announce" in response.content or b"d4:info" in response.content:
                return response.content
            logger.error(f"MAM torrent download returned non-torrent content for {torrent_id}")
            return None
        except Exception as e:
            logger.error(f"MAM torrent download failed: {e}")
            return None
