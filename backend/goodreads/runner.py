"""Wiring and entrypoints for the Goodreads pipeline.

Runs as its own small deployment rather than inside the web pod, so a slow feed
or a stuck download cannot affect the interactive search UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from backend.goodreads.feed import FeedError, FeedStatus, fetch_all, fetch_shelf
from backend.goodreads.annas_source import AnnasSource
from backend.goodreads.store import MemorySeenStore, PostgresSeenStore
from backend.goodreads.sync import DELAY_BETWEEN_BOOKS_S, GoodreadsSync
from backend.libgen import LibGenScraper

logger = logging.getLogger(__name__)

GOODREADS_USER_ID = os.getenv("GOODREADS_USER_ID", "33074940")
GOODREADS_SHELF = os.getenv("GOODREADS_SHELF", "to-read")
POLL_SECONDS = int(os.getenv("GOODREADS_POLL_SECONDS", "120"))
DOWNLOADS_ENABLED = os.getenv("GOODREADS_DOWNLOADS_ENABLED", "false").lower() == "true"
DATABASE_URL = os.getenv("GOODREADS_DATABASE_URL", "")
BOOK_SEARCH_URL = os.getenv(
    "BOOK_SEARCH_URL", "http://book-search.ebooks.svc.cluster.local"
)
API_KEY = os.getenv("API_KEY", "")
SHELF_ID = int(os.getenv("GOODREADS_SHELF_ID", "0") or 0)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# How often to fetch the shelf WITHOUT the conditional header. A quiet shelf
# answers 304 forever, and items are only re-examined on a 200 — so without this
# a book left pending by a transient failure, or claimed by a pod that died, is
# never looked at again until she happens to add something.
FULL_REFRESH_SECONDS = int(os.getenv("GOODREADS_FULL_REFRESH_SECONDS", "900"))

# A repeated failure (feed down, source down) should say so once, not every cycle.
_last_error: str | None = None


def etag_to_send(etag: str | None, last_full: float, now: float,
                 interval: float = FULL_REFRESH_SECONDS) -> str | None:
    """The validator to send this cycle, or None to force a full fetch."""
    if etag is None:
        return None
    if now - last_full >= interval:
        return None
    return etag


def build_notifier(client: httpx.AsyncClient):
    async def notify(text: str) -> None:
        if not SLACK_WEBHOOK_URL:
            logger.info("[slack] %s", text)
            return
        await client.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)

    return notify


def build_ingest(client: httpx.AsyncClient):
    async def ingest(*, md5: str, title: str, author: str) -> dict:
        response = await client.post(
            f"{BOOK_SEARCH_URL}/api/goodreads/ingest",
            json={"md5": md5, "title": title, "author": author, "shelf_id": SHELF_ID},
            headers={"X-Api-Key": API_KEY},
            timeout=600,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    return ingest


def build_store():
    if not DATABASE_URL:
        logger.warning("GOODREADS_DATABASE_URL unset — using in-memory state")
        return MemorySeenStore()
    store = PostgresSeenStore(DATABASE_URL)
    store.ensure_schema()
    return store


async def poll_forever() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Goodreads poller starting: user=%s shelf=%s every %ss (downloads %s)",
        GOODREADS_USER_ID, GOODREADS_SHELF, POLL_SECONDS,
        "ENABLED" if DOWNLOADS_ENABLED else "disabled",
    )

    store = build_store()
    # libgen is the primary: it is the source we can actually download from.
    # Anna's Archive is a FALLBACK, asked only for books libgen could not match —
    # it reaches more collections, but only works while a human-passed captcha
    # session is fresh in the shared browser (~20 minutes), so it is used
    # sparingly and drops out quietly when challenged again.
    source = LibGenScraper()
    fallback = AnnasSource()
    etag: str | None = None

    async with httpx.AsyncClient(follow_redirects=True) as client:
        sync = GoodreadsSync(
            source=source,
            fallback_source=fallback,
            ingest=build_ingest(client),
            store=store,
            notify=build_notifier(client),
            downloads_enabled=DOWNLOADS_ENABLED,
            delay_s=DELAY_BETWEEN_BOOKS_S,
        )

        # Seeding reads the whole shelf, not just the newest page: everything
        # already there is history, and recording only the first 100 would leave
        # the older tail looking new later.
        if store.is_empty():
            try:
                everything = await fetch_all(client, GOODREADS_USER_ID, GOODREADS_SHELF)
                seeded = await sync.process(everything)
                logger.info("Seeded %d existing shelf items", seeded.seeded)
            except Exception as exc:
                logger.exception("Seeding failed; will retry on the next cycle: %s", exc)

        last_full = time.monotonic()

        while True:
            try:
                send = etag_to_send(etag, last_full, time.monotonic())
                if send is None:
                    last_full = time.monotonic()
                result = await fetch_shelf(
                    client, GOODREADS_USER_ID, GOODREADS_SHELF, etag=send,
                )
                _clear_error()
                if result.status is FeedStatus.OK:
                    etag = result.etag or etag
                    outcome = await sync.process(result.items)
                    if outcome.seeded:
                        logger.info("Seeded %d existing items", outcome.seeded)
                    elif any([outcome.downloaded, outcome.missed, outcome.errors,
                              outcome.would_download]):
                        logger.info(
                            "cycle: %d downloaded, %d missed, %d errors, %d would-download",
                            outcome.downloaded, outcome.missed, outcome.errors,
                            outcome.would_download,
                        )
            except FeedError as exc:
                await _report_once(client, f"Goodreads feed unreadable: {exc}")
            except Exception as exc:
                logger.exception("Poll cycle failed")
                await _report_once(client, f"Goodreads poller error: {exc}")

            await asyncio.sleep(POLL_SECONDS)


def _clear_error() -> None:
    global _last_error
    _last_error = None


async def _report_once(client: httpx.AsyncClient, message: str) -> None:
    """Post a failure the first time it appears, then stay quiet until it clears."""
    global _last_error
    logger.error(message)
    if message == _last_error:
        return
    _last_error = message
    if SLACK_WEBHOOK_URL:
        try:
            await client.post(SLACK_WEBHOOK_URL, json={"text": f"⚠️ {message}"}, timeout=10)
        except Exception as exc:
            logger.warning("Could not report to Slack: %s", exc)


if __name__ == "__main__":
    asyncio.run(poll_forever())
