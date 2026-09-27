"""The poller's side of the Claude check, and retries for empty libgen answers.

The ingest endpoint runs the checks and answers "rejected" when Claude says a
file is not the book. The poller then moves to the next confident candidate, at
most three, and a book whose candidates were all refused is reported once, with
Claude's reason, so it cannot pass for a plain "not found".
"""

from backend.goodreads.sources import SourceUnavailable
from backend.goodreads.store import MemorySeenStore, Outcome
from backend.goodreads.sync import MAX_ATTEMPTS, MAX_CANDIDATES, GoodreadsSync
from tests.test_goodreads_sync import FakeNotifier, candidate, shelf_item


class ScriptedIngest:
    """Answers each call from a script, keyed by md5."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def __call__(self, *, md5, title, author, details=None):
        self.calls.append({"md5": md5, "details": details})
        answer = self.answers.get(md5, {"status": "ok", "book_id": 600})
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


class CountingSource:
    def __init__(self, candidates, isbn_candidates=None):
        self.candidates = candidates
        self.isbn_candidates = isbn_candidates or []

    async def search_by_isbn(self, isbn):
        return list(self.isbn_candidates)

    async def search_candidates(self, query):
        return list(self.candidates)


async def seeded_sync(source, ingest):
    store = MemorySeenStore()
    store.mark_seeded(["seed"])
    notifier = FakeNotifier()
    sync = GoodreadsSync(source=source, ingest=ingest, store=store,
                         notify=notifier, downloads_enabled=True)
    return sync, store, notifier


def four_copies():
    return [candidate(title="Romanov", author="Nadine Brandes", md5=str(n) * 32)
            for n in range(1, 5)]


async def test_a_rejected_file_moves_on_to_the_next_candidate():
    first, second, *_ = four_copies()
    ingest = ScriptedIngest({first.md5: {"status": "rejected", "stage": "file",
                                         "reason": "an abridged sample"}})
    sync, store, notifier = await seeded_sync(CountingSource([first, second]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    result = await sync.process([book])

    assert [c["md5"] for c in ingest.calls] == [first.md5, second.md5]
    assert result.downloaded == 1
    assert store.outcome("2") == Outcome.DOWNLOADED
    assert len(notifier.messages) == 1
    assert "📖" in notifier.messages[0]


async def test_the_ingest_is_given_what_claude_needs_to_judge():
    [copy] = four_copies()[:1]
    ingest = ScriptedIngest({})
    sync, _, _ = await seeded_sync(CountingSource([copy]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn="0785217240")
    book.description = "The history books say I died."
    book.published = "2019"

    await sync.process([book])

    details = ingest.calls[0]["details"]
    assert details["isbn"] == "0785217240"
    assert details["description"] == "The history books say I died."
    assert details["published"] == "2019"
    assert details["candidate"]["title"] == "Romanov"
    assert details["candidate"]["ext"] == "epub"


async def test_at_most_three_candidates_are_tried_and_the_miss_names_claudes_reason():
    copies = four_copies()
    ingest = ScriptedIngest({c.md5: {"status": "rejected", "stage": "record",
                                     "reason": f"not the novel ({n})"}
                             for n, c in enumerate(copies)})
    sync, store, notifier = await seeded_sync(CountingSource(copies), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    result = await sync.process([book])

    assert len(ingest.calls) == MAX_CANDIDATES == 3
    assert store.outcome("2") == Outcome.REJECTED
    assert result.missed == 1
    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert "Claude" in message
    assert "not the novel (2)" in message
    assert "annas-archive" in message


async def test_a_claude_outage_defers_the_book_instead_of_spending_it():
    [copy] = four_copies()[:1]
    ingest = ScriptedIngest({copy.md5: SourceUnavailable("Claude check unavailable: queue full")})
    sync, store, notifier = await seeded_sync(CountingSource([copy]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    result = await sync.process([book])

    assert result.deferred == 1
    assert store.outcome("2") == Outcome.PENDING
    assert notifier.messages == []


async def test_a_duplicate_stops_the_loop_at_once():
    first, second, *_ = four_copies()
    ingest = ScriptedIngest({first.md5: {"status": "duplicate"}})
    sync, store, notifier = await seeded_sync(CountingSource([first, second]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    await sync.process([book])

    assert len(ingest.calls) == 1
    assert store.outcome("2") == Outcome.OWNED


# --------------------------------------------------------------------------- #
# An empty answer is retried on a later cycle                                  #
# --------------------------------------------------------------------------- #

async def test_an_empty_answer_is_retried_on_later_cycles_before_it_counts():
    """libgen answers some searches with an empty page and the same search
    seconds later with six files. The page looks identical to a real absence,
    so only time tells them apart."""
    sync, store, notifier = await seeded_sync(CountingSource([]), ScriptedIngest({}))
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn="0785217240")

    for _ in range(MAX_ATTEMPTS - 1):
        result = await sync.process([book])
        assert result.deferred == 1
        assert store.outcome("2") == Outcome.PENDING
        assert notifier.messages == []

    result = await sync.process([book])

    assert result.missed == 1
    assert store.outcome("2") == Outcome.NOT_FOUND
    assert len(notifier.messages) == 1
    assert "🔎" in notifier.messages[0]


async def test_an_empty_isbn_answer_with_no_title_match_is_retried_too():
    stranger = candidate(title="Romanov Dynasty: A History", author="Someone Else")
    sync, store, _ = await seeded_sync(CountingSource([stranger]), ScriptedIngest({}))
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn="0785217240")

    result = await sync.process([book])

    assert result.deferred == 1
    assert store.outcome("2") == Outcome.PENDING


async def test_a_book_found_by_title_is_not_held_back_by_an_empty_isbn_answer():
    [copy] = four_copies()[:1]
    ingest = ScriptedIngest({})
    sync, store, _ = await seeded_sync(CountingSource([copy]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn="0785217240")

    result = await sync.process([book])

    assert result.downloaded == 1
    assert store.outcome("2") == Outcome.DOWNLOADED


async def test_a_miss_with_a_real_isbn_answer_is_final_straight_away():
    """When libgen did answer, and answered with something else, waiting will
    not change it."""
    italian = candidate(title="Romanov", author="Nadine Brandes")
    italian.language = "Italian"
    sync, store, _ = await seeded_sync(CountingSource([italian], isbn_candidates=[italian]),
                                       ScriptedIngest({}))
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn="0785217240")

    result = await sync.process([book])

    assert result.missed == 1
    assert store.outcome("2") == Outcome.NO_MATCH


# --------------------------------------------------------------------------- #
# A download that keeps breaking moves on to the next file                     #
# --------------------------------------------------------------------------- #

async def test_a_failed_download_tries_the_next_candidate_in_the_same_cycle():
    """libgen's mirror answered 503 three times for Romanov's first file on
    2026-09-27 while five other copies of the same book were on offer."""
    first, second, *_ = four_copies()
    ingest = ScriptedIngest({first.md5: RuntimeError("HTTP 502: download failed")})
    sync, store, notifier = await seeded_sync(CountingSource([first, second]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    result = await sync.process([book])

    assert [c["md5"] for c in ingest.calls] == [first.md5, second.md5]
    assert result.downloaded == 1
    assert store.outcome("2") == Outcome.DOWNLOADED


async def test_when_every_candidate_fails_to_download_the_book_waits_for_a_later_cycle():
    first, second, *_ = four_copies()
    ingest = ScriptedIngest({first.md5: RuntimeError("HTTP 502: download failed"),
                             second.md5: RuntimeError("HTTP 502: download failed")})
    sync, store, notifier = await seeded_sync(CountingSource([first, second]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    result = await sync.process([book])

    assert result.deferred == 1
    assert store.outcome("2") == Outcome.PENDING
    assert notifier.messages == []


async def test_a_refusal_plus_a_failed_download_is_not_a_final_refusal():
    """One file was refused, the other never arrived: the second might still
    be the book, so the book waits rather than being reported as refused."""
    first, second, *_ = four_copies()
    ingest = ScriptedIngest({first.md5: {"status": "rejected", "stage": "file", "reason": "sample"},
                             second.md5: RuntimeError("HTTP 502: download failed")})
    sync, store, notifier = await seeded_sync(CountingSource([first, second]), ingest)
    book = shelf_item("2", title="Romanov", author="Nadine Brandes", isbn=None)

    result = await sync.process([book])

    assert result.deferred == 1
    assert store.outcome("2") == Outcome.PENDING
