"""Forwarding a Goodreads-ingested book to the Kindle, and what Slack is told.

The alert contract is one line per book, so every case here asserts the count as
well as the content: a book that reached the Kindle, one deliberately left off it,
and one where the send failed and Viktor has to do something.
"""

from tests.test_goodreads_sync import FakeIngest, build, candidate, shelf_item

from backend.goodreads.store import Outcome


async def run_one_new_book(ingest_result):
    """Seed, then add one genuinely new book and process it."""
    seeded = [shelf_item("1")]
    ingest = FakeIngest(result=ingest_result)
    sync, source, ingest, notifier, _ = build(
        seeded, [candidate(title="Neuromancer", author="William Gibson")],
        ingest=ingest,
    )
    await sync.process(seeded)  # seeding run

    new = shelf_item("2", title="Neuromancer", author="William Gibson", isbn=None)
    result = await sync.process(seeded + [new])
    return result, notifier, sync


async def test_a_book_that_reached_the_kindle_says_so_in_one_message():
    result, notifier, sync = await run_one_new_book(
        {"status": "ok", "book_id": 501, "kindle_sent": True,
         "kindle_error": None, "kindle_skipped": None},
    )

    assert result.downloaded == 1
    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert "Neuromancer" in message
    assert "Kindle" in message
    assert sync.store.outcome("2") == Outcome.DOWNLOADED


async def test_a_pdf_only_book_is_shelved_and_the_message_explains_the_skip():
    """Still one message, and it must not read as a failure — nothing is wrong."""
    result, notifier, _ = await run_one_new_book(
        {"status": "ok", "book_id": 501, "kindle_sent": False,
         "kindle_error": None, "kindle_skipped": "pdf does not reflow on a Kindle"},
    )

    assert result.downloaded == 1
    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert "pdf does not reflow on a Kindle" in message
    assert "⚠️" not in message
    assert "failed" not in message.lower()


async def test_a_failed_kindle_send_is_flagged_as_needing_attention():
    result, notifier, _ = await run_one_new_book(
        {"status": "ok", "book_id": 501, "kindle_sent": False,
         "kindle_error": "SMTP timeout", "kindle_skipped": None},
    )

    # The book itself arrived, so it is not a download failure.
    assert result.downloaded == 1
    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert "SMTP timeout" in message
    assert "⚠️" in message
    # The book is safe in Calibre; the message must say where it is.
    assert "shelf" in message.lower()


async def test_a_book_already_in_calibre_is_not_forwarded():
    """Viktor's call: only books we actually fetch go to the Kindle, because an
    owned book may already be on the device."""
    result, notifier, sync = await run_one_new_book({"status": "duplicate"})

    assert len(notifier.messages) == 1
    assert "Kindle" not in notifier.messages[0]
    assert sync.store.outcome("2") == Outcome.OWNED


async def test_an_ingest_response_without_kindle_fields_still_reports_cleanly():
    """The endpoint may have Kindle forwarding switched off entirely."""
    result, notifier, _ = await run_one_new_book({"status": "ok", "book_id": 501})

    assert result.downloaded == 1
    assert len(notifier.messages) == 1
    assert "Neuromancer" in notifier.messages[0]
    assert "⚠️" not in notifier.messages[0]
