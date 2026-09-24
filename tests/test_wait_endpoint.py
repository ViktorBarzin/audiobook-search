"""The phone waits for the answer in short holds, and always gets text back.

iOS gives up on "Get Contents of URL" at about 25 s and Traefik at 30 s, so the
shortcut asks up to six times and the server holds each ask for at most
WAIT_HOLD_SECONDS. The job id rides in the X-Job-Id header, because only the
first variable attachment in a shortcut's header dictionary resolves and a
variable inside a URL does not. Whatever the shortcut last received is what
Viktor sees, so every answer is plain text meant to be read, never a bare 404.
"""

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main

ANCA = "ancaelena98_4RMJsy@kindle.com"


@pytest.fixture
def jobs(monkeypatch):
    table = {}
    monkeypatch.setattr(bs_main, "_download_jobs", table)
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    monkeypatch.setattr(bs_main, "WAIT_HOLD_SECONDS", 0.2)
    return table


@pytest.fixture
def client(jobs):
    return TestClient(bs_main.app)


def running(**extra):
    job = {"status": "downloading", "phase": "fetching", "title": "Remember Me?",
           "author": "Sophie Kinsella", "md5": "d" * 32, "message": "",
           "kindle_email": ANCA, "finished": False}
    job.update(extra)
    return job


def test_a_finished_job_answers_at_once(client, jobs):
    jobs["j1"] = running(finished=True, outcome="done",
                         final_text="✅ Remember Me? → Anca's Kindle (epub, 28 s)")

    r = client.get("/api/download-status/wait", headers={"X-Job-Id": "j1"})

    assert r.status_code == 200
    assert r.text == "✅ Remember Me? → Anca's Kindle (epub, 28 s)"


def test_a_running_job_answers_with_progress_after_the_hold(client, jobs):
    jobs["j2"] = running()

    r = client.get("/api/download-status/wait", headers={"X-Job-Id": "j2"})

    assert r.status_code == 200
    assert r.text.startswith("⏳")
    assert "Remember Me?" in r.text


def test_an_unknown_job_is_explained_not_404d(client, jobs):
    r = client.get("/api/download-status/wait", headers={"X-Job-Id": "gone"})

    assert r.status_code == 200
    assert "Slack" in r.text


def test_no_job_id_is_explained_too(client, jobs):
    r = client.get("/api/download-status/wait")

    assert r.status_code == 200
    assert r.text.startswith("⚠️")


def test_the_job_id_may_also_come_in_the_query_string(client, jobs):
    jobs["j3"] = running(finished=True, outcome="failed", final_text="⚠️ nope")

    r = client.get("/api/download-status/wait?job_id=j3")

    assert r.text == "⚠️ nope"


def test_a_long_conversion_hands_over_without_holding(client, jobs, monkeypatch):
    """Once a PDF is converting, the phone is told to stop waiting."""
    monkeypatch.setattr(bs_main, "WAIT_HOLD_SECONDS", 30)
    jobs["j4"] = running(phase="converting", status="importing")
    started = time.monotonic()

    r = client.get("/api/download-status/wait", headers={"X-Job-Id": "j4"})

    assert time.monotonic() - started < 5, "it held the phone instead of handing over"
    assert r.status_code == 200
    assert r.text.startswith("⏳")
    assert "Slack" in r.text


def test_the_last_wait_points_at_slack(client, jobs):
    """The shortcut marks its final ask with a literal header."""
    jobs["j7"] = running()

    r = client.get("/api/download-status/wait",
                   headers={"X-Job-Id": "j7", "X-Last-Wait": "1"})

    assert r.text.startswith("⏳")
    assert "Slack" in r.text


def test_the_wait_route_is_not_swallowed_by_the_job_route(client, jobs):
    """'wait' must not be read as a job id and 404 as 'Job not found'."""
    r = client.get("/api/download-status/wait", headers={"X-Job-Id": "nope"})

    assert "Job not found" not in r.text


def test_the_json_status_route_still_works(client, jobs):
    jobs["j5"] = running(finished=True, outcome="done", final_text="✅ done",
                         pre_existing={"a.epub"})
    bs_main._job_events["j5"] = asyncio.Event()

    r = client.get("/api/download-status/j5")

    assert r.status_code == 200
    assert r.json()["final_text"] == "✅ done"


async def test_a_job_that_finishes_during_the_hold_answers_straight_away(jobs, monkeypatch):
    monkeypatch.setattr(bs_main, "WAIT_HOLD_SECONDS", 10)
    jobs["j6"] = running()

    async def finish_soon():
        await asyncio.sleep(0.05)
        jobs["j6"]["outcome"] = "done"
        jobs["j6"]["final_text"] = "✅ Remember Me? → Anca's Kindle (epub, 12 s)"
        bs_main._mark_finished("j6")

    transport = httpx.ASGITransport(app=bs_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        finisher = asyncio.create_task(finish_soon())
        r = await asyncio.wait_for(
            client.get("/api/download-status/wait", headers={"X-Job-Id": "j6"}), timeout=5,
        )
        await finisher

    assert r.text == "✅ Remember Me? → Anca's Kindle (epub, 12 s)"
