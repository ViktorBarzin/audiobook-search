"""Anna's Archive as a discovery source for the pipeline.

AA indexes more than libgen does, so it is worth asking. Two limits are worth
stating plainly:

- **It is unreachable from this network.** Every AA domain answers 403 behind
  DDoS-Guard, direct and through the UK VPN exit alike (re-checked 2026-08-16).
  This source therefore contributes nothing today; it is wired so that it starts
  contributing by itself if that changes, and costs one cached probe meanwhile.
- **Discovery only.** The pipeline's single working fetch route is libgen's
  keyed `ads.php` → `get.php` by md5, so an AA record that libgen does not also
  hold cannot be downloaded: AA's own `slow_download` is challenge-gated and
  `fast_download` is paid. AA therefore helps for books libgen has but our
  libgen *search* missed, not for books libgen lacks.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from backend.goodreads.aa_domains import working_domain
from backend.goodreads.isbn import to_isbn13
from backend.goodreads.matcher import Candidate
from backend.goodreads.sources import SourceUnavailable, parse_size_bytes

logger = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_MD5_HREF_RE = re.compile(r"^/md5/([a-f0-9]{32})$", re.IGNORECASE)
_EXT_RE = re.compile(r"\b(epub|azw3|mobi|fb2|pdf)\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"[\d.]+\s*[kmg]?b\b", re.IGNORECASE)


class AnnasSource:
    """Search Anna's Archive, returning Candidates the matcher can judge."""

    TIMEOUT = 30.0

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=self.TIMEOUT, headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._domain: str | None = None
        self._domain_checked = False

    async def close(self):
        await self.client.aclose()

    async def _base(self) -> str:
        """Resolve a domain that actually serves results, or report an outage.

        working_domain() caches for 24h, including the negative answer, so a
        blocked Anna's Archive costs one probe a day rather than one per book.
        """
        if not self._domain_checked:
            self._domain = await working_domain(self.client)
            self._domain_checked = True
        if not self._domain:
            raise SourceUnavailable("no reachable Anna's Archive domain")
        return f"https://{self._domain}"

    async def search_by_isbn(self, isbn: str | None) -> list[Candidate]:
        isbn13 = to_isbn13(isbn)
        if not isbn13:
            return []
        return await self._search(isbn13)

    async def search_candidates(self, query: str) -> list[Candidate]:
        return await self._search(query)

    async def _search(self, query: str) -> list[Candidate]:
        base = await self._base()
        url = (f"{base}/search?q={httpx.QueryParams({'q': query})['q']}"
               "&content=book_nonfiction&content=book_fiction"
               "&ext=epub&ext=pdf&ext=mobi&lang=en")
        try:
            response = await self.client.get(url)
            response.raise_for_status()
        except Exception as exc:
            # Never return [] here: an empty list reads as "this book does not
            # exist" and would spend the book's one attempt on a blocked source.
            raise SourceUnavailable(f"Anna's Archive search failed: {exc}") from exc

        return self._parse(response.text)

    @staticmethod
    def _parse(html: str) -> list[Candidate]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[Candidate] = []

        for link in soup.find_all("a", href=True):
            match = _MD5_HREF_RE.match(link["href"].strip())
            if not match:
                continue

            lines = [ln.strip() for ln in link.get_text("\n").split("\n") if ln.strip()]
            if not lines:
                continue

            blob = " ".join(lines)
            ext = _EXT_RE.search(blob)
            size = _SIZE_RE.search(blob)

            candidates.append(Candidate(
                md5=match.group(1).lower(),
                title=lines[0],
                author=lines[1] if len(lines) > 1 else None,
                ext=(ext.group(1).lower() if ext else "epub"),
                # AA search exposes no per-result language; the query pins
                # lang=en, which is the only signal available here.
                language="English",
                size_bytes=parse_size_bytes(size.group(0) if size else None),
                source="annas",
            ))

        return candidates
