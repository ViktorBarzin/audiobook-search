import json
import logging
import re as _re
from urllib.parse import quote
import httpx

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)


class MAMScraper:
    """MyAnonamouse private tracker search."""

    BASE_URL = "https://www.myanonamouse.net"
    SEARCH_URL = f"{BASE_URL}/tor/js/loadSearchJSONbasic.php"
    TIMEOUT = 15.0
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._logged_in = False
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            headers={"User-Agent": self.USER_AGENT},
            follow_redirects=True,
        )

    async def _login(self):
        """Login to MAM and obtain session cookie."""
        if self._logged_in:
            return True

        try:
            # Step 1: GET login page for CSRF tokens
            r = await self.client.get(f"{self.BASE_URL}/login.php")
            t_match = _re.search(r'name="t" value="([^"]+)"', r.text)
            a_match = _re.search(r'name="a" value="([^"]+)"', r.text)
            if not t_match or not a_match:
                logger.error("MAM login: could not find CSRF tokens")
                return False

            t_val = t_match.group(1)
            a_val = a_match.group(1)

            # Step 2: POST login (j = length of t value, added by JS)
            data = {
                "email": self.email,
                "password": self.password,
                "t": t_val,
                "a": a_val,
                "j": str(len(t_val)),
                "rememberMe": "yes",
            }
            r2 = await self.client.post(f"{self.BASE_URL}/takelogin.php", data=data)

            # Check if we landed on a logged-in page
            if "/login.php" in str(r2.url):
                logger.error("MAM login failed - redirected back to login")
                return False

            self._logged_in = True
            logger.info("MAM login successful")
            return True
        except Exception as e:
            logger.error(f"MAM login failed: {e}")
            return False

    async def close(self):
        await self.client.aclose()

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search MAM for audiobooks."""
        if not await self._login():
            return []

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

        if not data.get("data"):
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

                narrator = None
                narrator_info = item.get("narrator_info")
                if narrator_info:
                    try:
                        narrators = json.loads(narrator_info) if isinstance(narrator_info, str) else narrator_info
                        if isinstance(narrators, dict):
                            narrator = ", ".join(narrators.values())
                    except (json.JSONDecodeError, TypeError):
                        pass

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
                category = item.get("catname", "")
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
        if not await self._login():
            return None

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

        if not data.get("data"):
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

        narrator = None
        narrator_info = item.get("narrator_info")
        if narrator_info:
            try:
                narrators = json.loads(narrator_info) if isinstance(narrator_info, str) else narrator_info
                if isinstance(narrators, dict):
                    narrator = ", ".join(narrators.values())
            except (json.JSONDecodeError, TypeError):
                pass

        filetype = item.get("filetype", "")
        category = item.get("catname", "")
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
        if not await self._login():
            return None

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
