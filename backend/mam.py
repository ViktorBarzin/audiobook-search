import json
import logging
import os
import re as _re
from urllib.parse import quote
import httpx

from backend.models import AudiobookResult, AudiobookDetail

logger = logging.getLogger(__name__)

# MAM API session cookie — NOT the browser lid cookie.
# Generate at MAM → Preferences → Security → create an ASN/IP-locked session
# with "allow dynamic seedbox IP" enabled. Copy the mam_id value.
MAM_ID = os.getenv("MAM_ID", "")

# MAM dynamic seedbox endpoint (t. subdomain, not www.)
SEEDBOX_URL = "https://t.myanonamouse.net/json/dynamicSeedbox.php"


class MAMScraper:
    """MyAnonamouse private tracker search using mam_id API session."""

    BASE_URL = "https://www.myanonamouse.net"
    SEARCH_URL = f"{BASE_URL}/tor/js/loadSearchJSONbasic.php"
    TIMEOUT = 15.0
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._logged_in = False
        self._seedbox_activated = False
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT,
            headers={"User-Agent": self.USER_AGENT},
            follow_redirects=True,
        )

    async def _login(self):
        """Authenticate with MAM using mam_id API session cookie."""
        if self._logged_in:
            return True

        if not MAM_ID:
            logger.error(
                "MAM_ID env var not set. Generate an API session at "
                "MAM → Preferences → Security (ASN/IP-locked, allow dynamic seedbox)."
            )
            return False

        # Set the mam_id cookie
        self.client.cookies.set("mam_id", MAM_ID, domain=".myanonamouse.net")

        # Step 1: Activate dynamic seedbox (register our IP)
        if not self._seedbox_activated:
            try:
                r = await self.client.get(SEEDBOX_URL)
                data = r.json()
                if data.get("Success"):
                    logger.info(f"MAM dynamic seedbox: {data.get('msg')} (IP: {data.get('ip')})")
                    self._seedbox_activated = True
                else:
                    msg = data.get("msg", "Unknown error")
                    logger.error(f"MAM dynamic seedbox failed: {msg}")
                    if "non-API session" in msg or "not allowed" in msg:
                        logger.error(
                            "The mam_id is a browser session, not an API session. "
                            "Create a new session at MAM → Preferences → Security "
                            "with 'allow dynamic seedbox IP' enabled."
                        )
                    return False
            except Exception as e:
                logger.error(f"MAM dynamic seedbox call failed: {e}")
                return False

        # Step 2: Verify search works
        try:
            r = await self.client.get(
                self.SEARCH_URL,
                params={"tor[text]": "test", "perpage": "1"},
            )
            if r.status_code == 200:
                r.json()  # Will throw if not valid JSON
                self._logged_in = True
                logger.info("MAM API session authenticated successfully")
                return True
            else:
                logger.error(f"MAM search returned {r.status_code}: {r.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"MAM search verification failed: {e}")
            return False

    async def close(self):
        await self.client.aclose()

    async def search(self, query: str) -> list[AudiobookResult]:
        """Search MAM for audiobooks and ebooks."""
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

                # Detect content type from category
                content_type = "ebook" if category.lower().startswith("ebook") else "audiobook"

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
                    content_type=content_type,
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
        torrent_url = f"{self.BASE_URL}/tor/download.php?tid={torrent_id}"

        content_type = "ebook" if category.lower().startswith("ebook") else "audiobook"

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
            content_type=content_type,
        )

    async def download_torrent_file(self, torrent_id: str) -> bytes | None:
        """Download the .torrent file from MAM."""
        if not await self._login():
            return None

        url = f"{self.BASE_URL}/tor/download.php?tid={torrent_id}"
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
