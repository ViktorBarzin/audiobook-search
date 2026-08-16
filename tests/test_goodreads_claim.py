"""A book must be claimed before work starts, not after it finishes.

Live failure 2026-08-16: two poller pods (a manual restart racing a deploy)
each downloaded and imported "So Good They Can't Ignore You", producing books
499 and 500. The row is only written once the whole download-and-import
completes — minutes later — so the second pod saw no record and started again.
"""

from datetime import datetime, timezone

from backend.goodreads.matcher import ShelfItem
from backend.goodreads.store import MemorySeenStore, Outcome


def item(book_id="1"):
    return ShelfItem(book_id=book_id, title="Strange Houses", author="Uketsu",
                     isbn=None, added_at=datetime(2026, 8, 15, tzinfo=timezone.utc))


def test_only_one_worker_can_claim_a_book():
    store = MemorySeenStore()

    assert store.claim(item()) is True
    assert store.claim(item()) is False, "a second worker must not get the same book"


def test_a_claimed_book_is_not_offered_again():
    store = MemorySeenStore()
    store.claim(item("7"))

    assert "7" in store.known_ids(), "in-flight work must look handled to other cycles"


def test_a_stale_claim_can_be_taken_over():
    """A pod that died mid-download must not strand the book forever."""
    store = MemorySeenStore()
    store.claim(item("8"))
    assert "8" in store.known_ids()

    store.expire_claims(older_than_seconds=0)
    assert "8" not in store.known_ids(), "an abandoned claim is offered again"
    assert store.claim(item("8")) is True


def test_a_deferred_book_can_be_claimed_again():
    store = MemorySeenStore()
    store.claim(item("9"))
    store.defer(item("9"), "libgen timed out")

    assert store.claim(item("9")) is True


def test_a_finished_book_is_never_claimed_again():
    store = MemorySeenStore()
    store.claim(item("10"))
    store.record(item("10"), Outcome.DOWNLOADED, calibre_id=499)

    assert store.claim(item("10")) is False
    assert "10" in store.known_ids()


def test_a_seeded_book_is_never_claimed():
    store = MemorySeenStore()
    store.mark_seeded(["11"])

    assert store.claim(item("11")) is False
