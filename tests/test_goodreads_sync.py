"""Orchestration tests: seeding, one-shot semantics, and the download gate."""

from datetime import datetime, timezone

import pytest

from backend.goodreads.matcher import Candidate, ShelfItem
from backend.goodreads.store import MemorySeenStore
from backend.goodreads.sync import GoodreadsSync, Outcome


def shelf_item(book_id, title="Strange Houses", author="Uketsu", isbn="006343315X"):
    return ShelfItem(
        book_id=book_id, title=title, author=author, isbn=isbn,
        added_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )


def candidate(title="Strange Houses", author="Uketsu", md5="a" * 32):
    return Candidate(md5=md5, title=title, author=author, ext="epub",
                     language="English", size_bytes=900_000, source="libgen")


class FakeSource:
    def __init__(self, candidates=None):
        self.candidates = candidates if candidates is not None else []
        self.isbn_calls, self.text_calls = [], []

    async def search_by_isbn(self, isbn):
        self.isbn_calls.append(isbn)
        return []

    async def search_candidates(self, query):
        self.text_calls.append(query)
        return list(self.candidates)


class FakeIngest:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "ok", "book_id": 501}

    async def __call__(self, *, md5, title, author):
        self.calls.append(md5)
        return dict(self.result)


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def __call__(self, text):
        self.messages.append(text)


def build(items, candidates=None, downloads_enabled=True, store=None, ingest=None):
    source = FakeSource(candidates)
    ingest = ingest or FakeIngest()
    notifier = FakeNotifier()
    sync = GoodreadsSync(
        source=source,
        ingest=ingest,
        store=store or MemorySeenStore(),
        notify=notifier,
        downloads_enabled=downloads_enabled,
    )
    return sync, source, ingest, notifier, items


# --------------------------------------------------------------------------- #
# Seeding                                                                      #
# --------------------------------------------------------------------------- #

async def test_first_run_seeds_without_downloading():
    """576 books already on her shelf must not trigger 576 downloads."""
    items = [shelf_item(str(i)) for i in range(5)]
    sync, source, ingest, notifier, _ = build(items, [candidate()])

    processed = await sync.process(items)

    assert ingest.calls == []
    assert source.text_calls == []
    assert all(sync.store.outcome(i.book_id) == Outcome.SEEDED for i in items)
    assert processed.seeded == 5


async def test_second_run_downloads_only_genuinely_new_items():
    items = [shelf_item("1"), shelf_item("2")]
    sync, source, ingest, notifier, _ = build(items, [candidate()])
    await sync.process(items)          # seeding run

    new_item = shelf_item("3", title="Neuromancer", author="William Gibson", isbn=None)
    source.candidates = [candidate(title="Neuromancer", author="William Gibson")]
    result = await sync.process(items + [new_item])

    assert ingest.calls == ["a" * 32]
    assert result.downloaded == 1
    assert sync.store.outcome("3") == Outcome.DOWNLOADED


# --------------------------------------------------------------------------- #
# One-shot semantics                                                           #
# --------------------------------------------------------------------------- #

async def test_a_missed_book_is_never_retried():
    """One attempt per book: a miss is recorded and not searched again."""
    store = MemorySeenStore()
    store.mark_seeded(["0"])
    item = shelf_item("9", title="May We Feed the King", author="Rebecca Perry")
    sync, source, ingest, _, _ = build([item], candidates=[], store=store)

    await sync.process([item])
    assert sync.store.outcome("9") == Outcome.NOT_FOUND
    calls_after_first_attempt = len(source.text_calls)

    await sync.process([item])
    assert len(source.text_calls) == calls_after_first_attempt, (
        "a recorded miss must never be searched again"
    )


async def test_placeholder_titles_are_skipped_without_searching():
    store = MemorySeenStore()
    store.mark_seeded(["0"])
    item = shelf_item("7", title="Untitled (A Court of Thorns and Roses, #6)",
                      author="Sarah J. Maas", isbn=None)
    sync, source, ingest, _, _ = build([item], store=store)

    await sync.process([item])

    assert source.text_calls == []
    assert source.isbn_calls == []
    assert ingest.calls == []
    assert sync.store.outcome("7") == Outcome.SKIPPED


async def test_duplicate_reported_by_ingest_is_recorded_as_owned():
    store = MemorySeenStore()
    store.mark_seeded(["0"])
    item = shelf_item("11")
    ingest = FakeIngest({"status": "duplicate", "message": "already in Calibre"})
    sync, *_ = build([item], [candidate()], store=store, ingest=ingest)

    await sync.process([item])

    assert sync.store.outcome("11") == Outcome.OWNED


# --------------------------------------------------------------------------- #
# The download gate                                                            #
# --------------------------------------------------------------------------- #

async def test_gate_disabled_matches_but_never_downloads():
    """The validation gate: matching runs and is reported, downloads do not."""
    store = MemorySeenStore()
    store.mark_seeded(["0"])
    item = shelf_item("21")
    sync, source, ingest, notifier, _ = build(
        [item], [candidate()], downloads_enabled=False, store=store,
    )

    result = await sync.process([item])

    assert ingest.calls == []
    assert result.would_download == 1
    assert sync.store.outcome("21") is None, "a dry run must not consume the one attempt"
    assert any("would download" in m.lower() for m in notifier.messages)


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #

async def test_quiet_cycle_says_nothing():
    store = MemorySeenStore()
    store.mark_seeded(["1"])
    sync, _, _, notifier, _ = build([shelf_item("1")], store=store)

    await sync.process([shelf_item("1")])

    assert notifier.messages == []


async def test_reports_success_and_miss_once_each():
    store = MemorySeenStore()
    store.mark_seeded(["0"])
    found = shelf_item("31", title="Strange Houses", author="Uketsu")
    missing = shelf_item("32", title="May We Feed the King", author="Rebecca Perry")

    class SelectiveSource(FakeSource):
        async def search_candidates(self, query):
            self.text_calls.append(query)
            return [candidate()] if "Strange" in query else []

    sync = GoodreadsSync(
        source=SelectiveSource(), ingest=FakeIngest(), store=store,
        notify=(notifier := FakeNotifier()), downloads_enabled=True,
    )
    await sync.process([found, missing])

    assert len(notifier.messages) == 2
    assert any("Strange Houses" in m for m in notifier.messages)
    assert any("May We Feed the King" in m for m in notifier.messages)


async def test_ingest_failure_is_recorded_and_reported():
    store = MemorySeenStore()
    store.mark_seeded(["0"])
    item = shelf_item("41")

    class FailingIngest(FakeIngest):
        async def __call__(self, **kwargs):
            raise RuntimeError("libgen returned 503")

    sync, _, _, notifier, _ = build(
        [item], [candidate()], store=store, ingest=FailingIngest(),
    )
    await sync.process([item])

    assert sync.store.outcome("41") == Outcome.ERROR
    assert any("503" in m for m in notifier.messages)


# --------------------------------------------------------------------------- #
# Transient source failures                                                    #
#                                                                              #
# One attempt per book is about giving up on books that genuinely aren't out    #
# there. A libgen timeout is not that, and must not consume the one attempt --  #
# the first replay lost The Tokyo Zodiac Murders exactly this way, even though  #
# libgen had an English epub of it.                                            #
# --------------------------------------------------------------------------- #

async def test_source_outage_does_not_consume_the_attempt():
    from backend.goodreads.sources import SourceUnavailable

    store = MemorySeenStore()
    store.mark_seeded(["0"])
    item = shelf_item("51", title="The Tokyo Zodiac Murders", author="Soji Shimada")

    class FlakySource(FakeSource):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def search_by_isbn(self, isbn):
            self.isbn_calls.append(isbn)
            if self.fail:
                raise SourceUnavailable("libgen timed out")
            return [candidate(title="The Tokyo Zodiac Murders", author="Soji Shimada")]

        async def search_candidates(self, query):
            self.text_calls.append(query)
            if self.fail:
                raise SourceUnavailable("libgen timed out")
            return []

    source = FlakySource()
    sync = GoodreadsSync(source=source, ingest=(ingest := FakeIngest()), store=store,
                         notify=FakeNotifier(), downloads_enabled=True)

    await sync.process([item])
    assert sync.store.outcome("51") is None, "an outage must leave the book unhandled"
    assert ingest.calls == []

    source.fail = False
    await sync.process([item])
    assert sync.store.outcome("51") == Outcome.DOWNLOADED, "retried once the source is back"
