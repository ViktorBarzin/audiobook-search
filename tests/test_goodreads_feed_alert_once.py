"""A failure that is still unfixed posts to Slack once, not every cycle.

Live 2026-10-27..11-10: Goodreads answered the shelf feed with HTTP 404 on every
two-minute poll, and #alerts got the same "Goodreads feed unreadable" line 14
times. The old guard kept the last message in memory, so a pod restart re-posted
it, and so did a single different error in between (a connection blip on
2026-11-07 changed the message, and the next 404 counted as new). The latch now
lives in the store, so it survives restarts, and only a feed that reads again
re-arms it. Every cycle still logs the failure.
"""

import logging

import pytest

from backend.goodreads import runner
from backend.goodreads.store import MemorySeenStore


class FakeClient:
    def __init__(self):
        self.posts: list[str] = []

    async def post(self, url, json, timeout):
        self.posts.append(json["text"])


@pytest.fixture(autouse=True)
def webhook(monkeypatch):
    monkeypatch.setattr(runner, "SLACK_WEBHOOK_URL", "https://hooks.example/x")


async def test_a_repeating_feed_failure_posts_once():
    client, store = FakeClient(), MemorySeenStore()
    for _ in range(5):
        await runner.report_failure(client, store, "feed",
                                    "Goodreads feed unreadable: feed returned HTTP 404")
    assert len(client.posts) == 1
    assert "HTTP 404" in client.posts[0]


async def test_a_different_message_while_the_feed_is_still_down_stays_quiet():
    client, store = FakeClient(), MemorySeenStore()
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed request failed: ")
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    assert len(client.posts) == 1


async def test_a_restart_does_not_post_the_same_failure_again():
    store = MemorySeenStore()
    first, second = FakeClient(), FakeClient()
    await runner.report_failure(first, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    # A new process starts with fresh module state but the same store.
    await runner.report_failure(second, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    assert len(first.posts) == 1
    assert second.posts == []


async def test_the_feed_working_again_rearms_the_alert():
    client, store = FakeClient(), MemorySeenStore()
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    runner.clear_failure(store, "feed")
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    assert len(client.posts) == 2


async def test_a_poller_error_is_tracked_apart_from_the_feed():
    client, store = FakeClient(), MemorySeenStore()
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    await runner.report_failure(client, store, "poller", "Goodreads poller error: boom")
    await runner.report_failure(client, store, "poller", "Goodreads poller error: boom")
    assert len(client.posts) == 2


async def test_every_cycle_still_logs_the_failure(caplog):
    client, store = FakeClient(), MemorySeenStore()
    with caplog.at_level(logging.ERROR, logger=runner.logger.name):
        for _ in range(3):
            await runner.report_failure(client, store, "feed",
                                        "Goodreads feed unreadable: feed returned HTTP 404")
    assert sum("HTTP 404" in r.getMessage() for r in caplog.records) == 3


class BrokenStore(MemorySeenStore):
    def raise_alert(self, kind, message):
        raise RuntimeError("database unreachable")

    def clear_alert(self, kind):
        raise RuntimeError("database unreachable")


async def test_an_unreachable_store_still_posts_once_per_process():
    client, store = FakeClient(), BrokenStore()
    runner._memory_alerts.clear()
    for _ in range(3):
        await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    assert len(client.posts) == 1
    runner.clear_failure(store, "feed")
    await runner.report_failure(client, store, "feed", "Goodreads feed unreadable: feed returned HTTP 404")
    assert len(client.posts) == 2
