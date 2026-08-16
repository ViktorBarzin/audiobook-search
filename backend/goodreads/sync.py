"""Turn new Goodreads shelf additions into books in Calibre.

Shape of a cycle: read the shelf, keep the items we have never handled, try to
match each one, hand confident matches to the ingest endpoint, and record what
happened. There is no review queue, so every outcome is written down and anything
worth a human knowing is posted to Slack.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from backend.goodreads.matcher import (
    ShelfItem,
    author_surname,
    normalize_title,
    select_candidate,
)
from backend.goodreads.sources import SourceUnavailable
from backend.goodreads.store import Outcome

logger = logging.getLogger(__name__)

# Reasons the matcher returns, mapped to what we store.
_REASON_TO_OUTCOME = {
    "placeholder_title": Outcome.SKIPPED,
    "not_found": Outcome.NOT_FOUND,
    "no_english_edition": Outcome.NO_MATCH,
    "no_confident_match": Outcome.NO_MATCH,
}

# Keeps a burst of additions from hammering libgen; she adds ~3 books a week, so
# this only ever matters on a backfill.
DELAY_BETWEEN_BOOKS_S = 2.0
MAX_PER_CYCLE = 10

# How many times a transient failure (a source outage, a truncated download, a
# failed import) may defer a book before we stop trying. The one-attempt rule is
# about books that are not out there; infrastructure hiccups are not that, but
# they still must not retry forever — libgen closed a connection mid-download on
# 2026-08-16 and an unbounded retry would have hammered it every two minutes.
MAX_ATTEMPTS = 3


def search_queries(item: ShelfItem) -> list[str]:
    """Queries to try, most selective first.

    libgen matches every word against the title and author columns, so passing a
    full name with diacritics ('Sōji Shimada') finds nothing at all. Normalizing
    to ASCII and using only the surname is what makes these searches land; the
    title-only form is the fallback for authors libgen credits differently.
    """
    title = normalize_title(item.title)
    surname = author_surname(item.author)
    if not title:
        return []
    queries = [f"{title} {surname}".strip()] if surname else []
    queries.append(title)
    return queries


@dataclass
class CycleResult:
    seeded: int = 0
    downloaded: int = 0
    missed: int = 0
    skipped: int = 0
    errors: int = 0
    would_download: int = 0
    deferred: int = 0  # source was down; retried next cycle
    messages: list[str] = field(default_factory=list)


class GoodreadsSync:
    def __init__(self, source, ingest, store, notify, downloads_enabled: bool = False,
                 delay_s: float = 0.0, max_per_cycle: int = MAX_PER_CYCLE):
        self.source = source
        self.ingest = ingest
        self.store = store
        self.notify = notify
        self.downloads_enabled = downloads_enabled
        self.delay_s = delay_s
        self.max_per_cycle = max_per_cycle

    async def _say(self, result: CycleResult, text: str) -> None:
        result.messages.append(text)
        try:
            await self.notify(text)
        except Exception as exc:  # notification must never break the pipeline
            logger.warning("Slack notification failed: %s", exc)

    async def process(self, items: list[ShelfItem]) -> CycleResult:
        result = CycleResult()

        # First ever run: everything already on the shelf is history, not a backlog.
        if self.store.is_empty():
            count = self.store.mark_seeded(items)
            result.seeded = count
            logger.info("Seeded %d existing shelf items; no downloads attempted", count)
            return result

        known = self.store.known_ids()
        fresh = [i for i in items if i.book_id not in known]
        if not fresh:
            return result

        for item in fresh[: self.max_per_cycle]:
            try:
                await self._process_one(item, result)
            except (SourceUnavailable, Exception) as exc:
                # Neither a source outage nor a failed download means "this book
                # does not exist", so it keeps its claim on being fetched — but
                # only for a bounded number of cycles.
                if not isinstance(exc, SourceUnavailable):
                    logger.exception("Failed processing %s", item.title)
                await self._defer_or_give_up(item, exc, result)
            if self.delay_s:
                await asyncio.sleep(self.delay_s)

        return result

    async def _defer_or_give_up(self, item: ShelfItem, exc: Exception,
                                result: CycleResult) -> None:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        attempts = self.store.defer(item, reason)

        if attempts < MAX_ATTEMPTS:
            logger.warning(
                "Deferring %r after attempt %d/%d: %s",
                item.title, attempts, MAX_ATTEMPTS, reason,
            )
            result.deferred += 1
            return

        self.store.record(item, Outcome.ERROR, reason=reason)
        result.errors += 1
        await self._say(
            result,
            f"⚠️ *{item.title}* — {item.author}: gave up after {attempts} attempts ({exc})",
        )

    async def _process_one(self, item: ShelfItem, result: CycleResult) -> None:
        candidates, isbn_md5s = await self._gather_candidates(item)
        match = select_candidate(item, candidates, isbn_matched_md5s=isbn_md5s)

        if match.candidate is None:
            outcome = _REASON_TO_OUTCOME.get(match.reason, Outcome.NO_MATCH)
            self.store.record(item, outcome, reason=match.reason)
            if outcome is Outcome.SKIPPED:
                result.skipped += 1
                return
            result.missed += 1
            await self._say(
                result,
                f"🔎 *{item.title}* — {item.author}: no copy found ({match.reason})",
            )
            return

        if not self.downloads_enabled:
            # Validation mode: report the pick but leave the book unhandled, so the
            # one attempt it is entitled to is still available once downloads are on.
            result.would_download += 1
            await self._say(
                result,
                f"🧪 would download *{item.title}* — {item.author} "
                f"[{match.reason}, {match.candidate.ext}, md5 {match.candidate.md5[:8]}]",
            )
            return

        response = await self.ingest(
            md5=match.candidate.md5, title=item.title, author=item.author,
        )

        if response.get("status") == "duplicate":
            self.store.record(item, Outcome.OWNED, reason="already in Calibre",
                              md5=match.candidate.md5)
            return

        book_id = response.get("book_id")
        self.store.record(item, Outcome.DOWNLOADED, reason=match.reason,
                          md5=match.candidate.md5, calibre_id=book_id)
        result.downloaded += 1
        await self._say(
            result,
            f"📖 *{item.title}* — {item.author} → Anca's shelf "
            f"({match.candidate.ext}, matched by {match.reason})",
        )

    async def _gather_candidates(self, item: ShelfItem):
        """Collect candidates, ISBN first.

        A placeholder title is discarded before any request goes out: those books
        are unpublished, so searching for them only produces load and noise.
        """
        from backend.goodreads.matcher import is_placeholder

        if is_placeholder(item.title):
            return [], set()

        candidates, isbn_md5s = [], set()

        if item.isbn:
            by_isbn = await self.source.search_by_isbn(item.isbn)
            candidates.extend(by_isbn)
            isbn_md5s = {c.md5 for c in by_isbn}

        for query in search_queries(item):
            candidates.extend(await self.source.search_candidates(query))
            if candidates:
                break

        deduped = {}
        for candidate in candidates:
            deduped.setdefault(candidate.md5, candidate)
        return list(deduped.values()), isbn_md5s
