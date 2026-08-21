"""Turn new Goodreads shelf additions into books in Calibre.

Shape of a cycle: read the shelf, keep the items we have never handled, try to
match each one, hand confident matches to the ingest endpoint, and record what
happened. There is no review queue, so every outcome is written down and anything
worth a human knowing is posted to Slack.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from backend.goodreads.matcher import (
    ShelfItem,
    author_surname,
    is_placeholder,
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


# Anna's Archive can't be searched automatically — it only answers a human in a
# browser — so a miss hands Viktor a ready-made search to open.
ANNAS_DOMAIN = "annas-archive.pk"


def annas_search_url(item: ShelfItem) -> str:
    """A search on Anna's Archive for this book, ready to click."""
    from urllib.parse import quote_plus

    title = re.sub(r"\s*\([^)]*\)\s*$", "", item.title or "").strip()
    query = f"{title} {author_surname(item.author)}".strip()
    return f"https://{ANNAS_DOMAIN}/search?q={quote_plus(query)}"


def format_miss(item: ShelfItem, reason: str) -> str:
    """The message for a book we could not deliver.

    It has a job to do: Viktor searches Anna's Archive by hand from here, so the
    search link and the ISBN matter more than the wording.
    """
    lines = [
        f"🔎 *{item.title}* — {item.author}",
        f"No copy found on LibGen ({reason}). "
        f"Search Anna's Archive: {annas_search_url(item)}",
    ]
    if item.isbn:
        lines.append(f"ISBN {item.isbn}")
    return "\n".join(lines)


def format_owned(item: ShelfItem) -> str:
    """She added a book the library already holds — nothing to fetch, but still news."""
    return f"📚 *{item.title}* — {item.author}: already in Calibre, nothing to fetch"


def format_success(item: ShelfItem, ext: str, reason: str,
                   kindle_sent: bool = False, kindle_error: str | None = None,
                   kindle_skipped: str | None = None) -> str:
    """The one line a delivered book gets.

    The Kindle clause is the part worth reading: a deliberate skip is news but
    not a problem, while a failed send leaves the book in Calibre and needs a
    person, so only that case carries a warning marker.
    """
    where = "Anca's shelf + Kindle" if kindle_sent else "Anca's shelf"
    line = f"📖 *{item.title}* — {item.author} → {where} ({ext}, matched by {reason})"

    if kindle_error:
        return f"{line} · ⚠️ Kindle send did not go through: {kindle_error}"
    if kindle_skipped:
        return f"{line} · not sent to Kindle: {kindle_skipped}"
    return line


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
    sent_to_kindle: int = 0
    messages: list[str] = field(default_factory=list)


class GoodreadsSync:
    def __init__(self, source, ingest, store, notify, downloads_enabled: bool = False,
                 delay_s: float = 0.0, max_per_cycle: int = MAX_PER_CYCLE,
                 fallback_source=None):
        self.source = source
        # Consulted only for books the primary could not confidently match.
        # Anna's Archive lives here: its session is human-maintained and lapses
        # about twenty minutes after someone passes its captcha, and it shares a
        # browser with other work — so it is asked rarely, and exactly where it
        # earns its keep.
        self.fallback_source = fallback_source
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
            # Claim before doing anything expensive. Recording the outcome only
            # after the download completes left a window of minutes in which a
            # restarted or redeployed worker saw no row and fetched the same
            # book again — which is how one book landed in Calibre twice.
            if not self.store.claim(item):
                logger.info("Skipping %r — another worker holds it", item.title)
                continue
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
        await self._say(result, "\n".join([
            f"⚠️ *{item.title}* — {item.author}",
            f"Gave up after {attempts} attempts ({exc}). "
            f"Search Anna's Archive: {annas_search_url(item)}",
        ]))

    async def _process_one(self, item: ShelfItem, result: CycleResult) -> None:
        candidates, isbn_md5s = await self._gather_candidates(item)
        match = select_candidate(item, candidates, isbn_matched_md5s=isbn_md5s)

        if match.candidate is None and self.fallback_source and not is_placeholder(item.title):
            extra, extra_isbn = await self._gather_candidates(
                item, source=self.fallback_source, required=False,
            )
            if extra:
                candidates = candidates + extra
                match = select_candidate(
                    item, candidates, isbn_matched_md5s=isbn_md5s | extra_isbn,
                )

        if match.candidate is None:
            outcome = _REASON_TO_OUTCOME.get(match.reason, Outcome.NO_MATCH)
            self.store.record(item, outcome, reason=match.reason)
            if outcome is Outcome.SKIPPED:
                result.skipped += 1
                return
            result.missed += 1
            await self._say(result, format_miss(item, match.reason))
            return

        if not self.downloads_enabled:
            # Validation mode: report the pick but leave the book unhandled, so the
            # one attempt it is entitled to is still available once downloads are on.
            self.store.release(item)
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
            await self._say(result, format_owned(item))
            return

        book_id = response.get("book_id")
        self.store.record(item, Outcome.DOWNLOADED, reason=match.reason,
                          md5=match.candidate.md5, calibre_id=book_id)
        result.downloaded += 1
        # Forwarding to the Kindle is the ingest endpoint's job — it holds the
        # SMTP credentials and knows which formats actually landed in Calibre —
        # so this only reports what it did. A response with none of these fields
        # means forwarding is switched off, and the line reads as it always did.
        if response.get("kindle_sent"):
            result.sent_to_kindle += 1
        await self._say(result, format_success(
            item, match.candidate.ext, match.reason,
            kindle_sent=bool(response.get("kindle_sent")),
            kindle_error=response.get("kindle_error"),
            kindle_skipped=response.get("kindle_skipped"),
        ))

    async def _gather_candidates(self, item: ShelfItem, source=None, required: bool = True):
        """Collect candidates from a source, ISBN first.

        A placeholder title is discarded before any request goes out: those books
        are unpublished, so searching for them only produces load and noise.

        `required=False` marks an optional source — an outage there is swallowed,
        because it must not turn a book the primary already answered for into a
        deferred one.
        """
        if is_placeholder(item.title):
            return [], set()

        source = source or self.source
        candidates, isbn_md5s = [], set()

        try:
            if item.isbn:
                by_isbn = await source.search_by_isbn(item.isbn)
                candidates.extend(by_isbn)
                isbn_md5s = {c.md5 for c in by_isbn}

            for query in search_queries(item):
                candidates.extend(await source.search_candidates(query))
                if candidates:
                    break
        except SourceUnavailable:
            if required:
                raise
            logger.info("Optional source unavailable for %r; continuing", item.title)
            return [], set()

        deduped = {}
        for candidate in candidates:
            deduped.setdefault(candidate.md5, candidate)
        return list(deduped.values()), isbn_md5s
