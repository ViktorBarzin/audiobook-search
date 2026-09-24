"""A book goes to a Kindle once a day, whichever path sends it.

Three paths email a Kindle: the shortcut's finally block, /api/send-to-kindle
and the Goodreads ingest. All three go through _send_to_kindle, which now
refuses a book the same recipient received in the last 24 hours. The key is the
book, not the file: normalised title plus author surname, read from the library
row, so a rescued EPUB and the original PDF of one novel count as one.

A refusal is its own result rather than an error. On 2026-09-24 a review of the
plan found that an error string here would have failed the job and started a $5
rescue agent over a book that had already arrived.
"""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from tests.fake_library import make_library

ANCA = "ancaelena98_4RMJsy@kindle.com"
SMOKE = "spam@viktorbarzin.me"


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = make_library(tmp_path / "library", [
        (511, "Remember Me?", "Sophie Kinsella", {"EPUB": 400_000}),
        (512, "Remember Me?", "Sophie Kinsella", {"PDF": 900_000}),
        (513, "Dune", "Frank Herbert", {"EPUB": 700_000}),
    ])
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(lib))
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA, "smoke": SMOKE})
    return lib


@pytest.fixture
def smtp(monkeypatch):
    """Serve every OPDS download and record mail instead of sending it."""
    monkeypatch.setattr(bs_main, "SMTP_USER", "calibre-web@viktorbarzin.me")
    monkeypatch.setattr(bs_main, "SMTP_PASS", "secret")
    sent = []
    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", lambda msg: sent.append(msg["To"]))

    real = httpx.AsyncClient

    def opds(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"B" * 5000)),
                    follow_redirects=True)

    monkeypatch.setattr(bs_main.httpx, "AsyncClient", opds)
    return sent


async def test_the_same_book_is_not_sent_twice(library, smtp):
    first = await bs_main._send_to_kindle(511, "Remember Me?", ANCA)
    second = await bs_main._send_to_kindle(511, "Remember Me?", ANCA)

    assert first is None
    assert isinstance(second, bs_main.AlreadySent)
    assert smtp == [ANCA]


async def test_another_file_of_the_same_book_counts_as_the_same_book(library, smtp):
    await bs_main._send_to_kindle(511, "Remember Me?", ANCA)

    again = await bs_main._send_to_kindle(512, "Remember Me? (PDF)", ANCA)

    assert isinstance(again, bs_main.AlreadySent)
    assert smtp == [ANCA]


async def test_another_recipient_or_another_book_still_goes(library, smtp):
    await bs_main._send_to_kindle(511, "Remember Me?", ANCA)

    assert await bs_main._send_to_kindle(511, "Remember Me?", SMOKE) is None
    assert await bs_main._send_to_kindle(513, "Dune", ANCA) is None
    assert smtp == [ANCA, SMOKE, ANCA]


async def test_a_day_later_it_may_go_again(library, smtp, monkeypatch):
    await bs_main._send_to_kindle(511, "Remember Me?", ANCA)
    later = time.time() + 25 * 3600
    monkeypatch.setattr(bs_main.time, "time", lambda: later)

    assert await bs_main._send_to_kindle(511, "Remember Me?", ANCA) is None


async def test_the_guard_survives_a_restart(library, smtp, monkeypatch, state_dir):
    await bs_main._send_to_kindle(511, "Remember Me?", ANCA)
    assert json.loads((state_dir / "sends.json").read_text())
    monkeypatch.setattr(bs_main, "_kindle_sends", {})  # a fresh process

    assert isinstance(await bs_main._send_to_kindle(511, "Remember Me?", ANCA), bs_main.AlreadySent)


async def test_a_failed_send_does_not_count(library, smtp, monkeypatch):
    def refused(msg):
        raise ConnectionError("relay said no")

    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", refused)
    failed = await bs_main._send_to_kindle(511, "Remember Me?", ANCA)
    assert failed and not isinstance(failed, bs_main.AlreadySent)

    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", lambda msg: smtp.append(msg["To"]))
    assert await bs_main._send_to_kindle(511, "Remember Me?", ANCA) is None


async def test_two_sends_at_once_email_once(library, smtp, monkeypatch):
    def slow(msg):
        time.sleep(0.05)
        smtp.append(msg["To"])

    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", slow)

    results = await asyncio.gather(
        bs_main._send_to_kindle(511, "Remember Me?", ANCA),
        bs_main._send_to_kindle(512, "Remember Me?", ANCA),
    )

    assert smtp == [ANCA]
    assert sum(isinstance(r, bs_main.AlreadySent) for r in results) == 1


async def test_a_send_queued_behind_a_failing_one_still_goes(library, smtp, monkeypatch):
    attempts = []

    def first_one_fails(msg):
        attempts.append(msg["To"])
        if len(attempts) == 1:
            time.sleep(0.05)
            raise ConnectionError("relay hiccup")
        smtp.append(msg["To"])

    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", first_one_fails)

    results = await asyncio.gather(
        bs_main._send_to_kindle(511, "Remember Me?", ANCA),
        bs_main._send_to_kindle(512, "Remember Me?", ANCA),
    )

    assert smtp == [ANCA], "the waiting send must not be told the book already went"
    assert results[1] is None


async def test_a_refused_share_ends_done_with_a_success_line(library, smtp, monkeypatch):
    posted = []

    async def record(text):
        posted.append(text)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(bs_main, "_post_slack", record)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", noop)
    await bs_main._send_to_kindle(511, "Remember Me?", ANCA)
    job = {"status": "done", "title": "Remember Me?", "author": "Sophie Kinsella",
           "md5": "d" * 32, "book_id": 512, "kindle_email": ANCA, "message": "Added to Calibre"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._settle_job("j")

    assert job["status"] == "done"
    assert job["outcome"] == "already_sent"
    assert "already sent to Anca's Kindle at" in job["final_text"]
    assert len(posted) == 1 and posted[0].startswith("✅")


@pytest.fixture
def api(library, smtp, monkeypatch):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    return TestClient(bs_main.app)


def send_api(api, **body):
    return api.post("/api/send-to-kindle", json=body, headers={"X-Api-Key": "test-key"})


def test_the_api_refuses_a_repeat_with_409(api):
    assert send_api(api, book_id=513, kindle_email=SMOKE).status_code == 200

    again = send_api(api, book_id=513, kindle_email=SMOKE)

    assert again.status_code == 409
    assert "already sent" in again.json()["detail"]


def test_the_api_only_mails_configured_recipients(api, smtp):
    r = send_api(api, book_id=513, kindle_email="stranger@example.com")

    assert r.status_code == 400
    assert smtp == []


def test_the_api_takes_a_recipient_name(api, smtp):
    assert send_api(api, book_id=513, deliver_to="smoke").status_code == 200
    assert smtp == [SMOKE]


async def test_goodreads_reports_a_repeat_as_skipped(library, smtp, monkeypatch):
    monkeypatch.setattr(bs_main, "GOODREADS_KINDLE_EMAIL", ANCA)
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    await bs_main._send_to_kindle(513, "Dune", ANCA)

    class Libgen:
        async def download_file(self, md5):
            return b"PK\x03\x04" + b"x" * 50_000, "Dune.epub"

    async def uploaded(data, filename):
        return 513

    async def not_a_duplicate(title, author):
        return None

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", uploaded)
    monkeypatch.setattr(bs_main, "_check_cwa_duplicate", not_a_duplicate)
    monkeypatch.setattr(bs_main, "GOODREADS_SHELF_ID", 0)

    r = TestClient(bs_main.app).post(
        "/api/goodreads/ingest", headers={"X-Api-Key": "test-key"},
        json={"md5": "e" * 32, "title": "Dune", "author": "Frank Herbert"},
    )

    body = r.json()
    assert body["kindle_sent"] is False
    assert body["kindle_error"] is None
    assert "already sent" in body["kindle_skipped"]
    assert smtp == [ANCA]
