"""One job sends one email.

Measured live 2026-09-07, running Anca's shortcut on the book page:

    10:03:42  Sent 'Obviously Awesome' to Kindle (ancaelena98_4RMJsy@kindle.com)
    10:03:44  Sent 'Obviously Awesome' to Kindle (ancaelena98_4RMJsy@kindle.com)

Two emails, two seconds apart, for one tap. _maybe_send_to_kindle is called on
the early-return paths in _process_download and again in its finally block, and
a return inside a try still runs finally. The comment on that finally already
says it is meant to be the single send point.

So the redundant calls go, and the send is made idempotent per job as well, so
a future call site cannot bring the duplicate back quietly.
"""

import pytest

import backend.main as bs_main

TITLE = "Obviously Awesome"
ADDRESS = "ancaelena98_4RMJsy@kindle.com"


@pytest.fixture
def sends(monkeypatch):
    """Records every attempt instead of sending."""
    calls = []

    async def fake_send(book_id, title, kindle_email, *a, **k):
        calls.append((book_id, title, kindle_email))
        return None

    monkeypatch.setattr(bs_main, "_send_to_kindle", fake_send)
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return calls


def a_finished_job(job_id="j1", **extra):
    job = {
        "md5": "5b6e6e722084ab2d8fdef68a30fe132b",
        "title": TITLE,
        "author": "April Dunford",
        "status": "done",
        "book_id": 507,
        "kindle_email": ADDRESS,
        "message": "Added to Calibre",
        "stage_detail": "",
    }
    job.update(extra)
    bs_main._download_jobs[job_id] = job
    return job


async def test_two_calls_send_one_email(sends):
    """This is the live duplicate, reproduced."""
    a_finished_job()

    await bs_main._maybe_send_to_kindle("j1", TITLE)
    await bs_main._maybe_send_to_kindle("j1", TITLE)

    assert len(sends) == 1, f"sent {len(sends)} times"
    assert sends[0] == (507, TITLE, ADDRESS)


async def test_the_first_call_still_sends(sends):
    a_finished_job()

    await bs_main._maybe_send_to_kindle("j1", TITLE)

    assert len(sends) == 1


async def test_two_different_jobs_each_send(sends):
    """Guarding one job must not suppress the next book."""
    a_finished_job("j1")
    a_finished_job("j2")

    await bs_main._maybe_send_to_kindle("j1", TITLE)
    await bs_main._maybe_send_to_kindle("j2", TITLE)

    assert len(sends) == 2


async def test_a_job_with_no_address_sends_nothing(sends):
    a_finished_job(kindle_email=None)

    await bs_main._maybe_send_to_kindle("j1", TITLE)

    assert sends == []


async def test_a_failed_job_sends_nothing(sends):
    a_finished_job(status="failed")

    await bs_main._maybe_send_to_kindle("j1", TITLE)

    assert sends == []


async def test_a_send_that_errored_is_not_retried_by_a_second_call(sends, monkeypatch):
    """The retry lives inside _send_to_kindle, not in being called twice."""
    attempts = []

    async def failing(book_id, title, kindle_email, *a, **k):
        attempts.append(kindle_email)
        return "SMTP said no"

    monkeypatch.setattr(bs_main, "_send_to_kindle", failing)
    a_finished_job()

    await bs_main._maybe_send_to_kindle("j1", TITLE)
    await bs_main._maybe_send_to_kindle("j1", TITLE)

    assert len(attempts) == 1
    assert bs_main._download_jobs["j1"]["status"] == "failed"


def test_the_download_path_sends_from_one_place_only():
    """The early returns used to call it, and finally ran anyway."""
    import inspect

    body = inspect.getsource(bs_main._process_download)

    assert body.count("_maybe_send_to_kindle(") == 1, (
        "delivery belongs in the finally block alone"
    )
