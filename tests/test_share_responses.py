"""Every answer to a share carries a `message` the phone can show.

The shortcut shows the reply's `message` first, then follows the job with
/api/download-status/wait. So a rejected share becomes a finished job of its
own (a tombstone) whose text is the real reason, and the waits repeat that
reason instead of contradicting it. Nothing on this route answers 5xx on
purpose: the ingress error-pages middleware replaces a 5xx body with HTML.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper

ANCA = "ancaelena98_4RMJsy@kindle.com"
MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"


async def _noop(*a, **k):
    return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    monkeypatch.setattr(bs_main, "_process_download", _noop)
    monkeypatch.setattr(bs_main, "_notify_slack", _noop)
    monkeypatch.setattr(bs_main, "_post_slack", _noop)
    monkeypatch.setattr(bs_main, "_detail_best_effort", _noop)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    monkeypatch.setattr(bs_main, "annas_scraper", AnnasArchiveScraper())
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return TestClient(bs_main.app)


def share(client, **headers):
    return client.post("/api/download-url", headers={"X-Api-Key": "test-key", **headers})


def waited(client, job_id):
    return client.get("/api/download-status/wait", headers={"X-Job-Id": job_id}).text


def test_a_share_for_anca_says_where_it_is_going(client):
    r = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome",
                         "X-Deliver-To": "anca"})

    assert r.status_code == 200
    assert r.json()["message"] == "📖 Queued: Obviously Awesome → Anca's Kindle"


def test_a_calibre_only_share_says_calibre(client):
    r = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})

    assert r.json()["message"] == "📖 Queued: Obviously Awesome → Calibre"


def test_a_page_with_no_book_gets_a_reason_the_waits_repeat(client):
    page = "<!DOCTYPE html><html><body>" + "x" * 1800 + "</body></html>"

    r = client.post("/api/download-url", content=page.encode(),
                    headers={"X-Api-Key": "test-key", "Content-Type": "text/html"})

    assert r.status_code == 400
    body = r.json()
    assert "finish loading" in body["detail"]
    assert body["message"].startswith("⚠️")
    assert "finish loading" in body["message"]
    assert waited(client, body["job_id"]) == body["message"]


def test_a_link_that_names_no_book_gets_a_reason_too(client):
    r = client.post("/api/download-url", json={"url": "https://annas-archive.org/search?q=ove"},
                    headers={"X-Api-Key": "test-key"})

    assert r.status_code == 400
    assert r.json()["message"].startswith("⚠️")
    assert waited(client, r.json()["job_id"]) == r.json()["message"]


def test_a_wrong_api_key_says_so_and_creates_nothing(client):
    r = client.post("/api/download-url", headers={"X-Api-Key": "wrong", "X-Book-Url": MD5})

    assert r.status_code == 401
    assert "API key" in r.json()["message"]
    assert bs_main._download_jobs == {}


def test_a_second_share_of_a_running_book_joins_it(client):
    first = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})
    second = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})

    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["message"].startswith("📖 Already on it")


def test_joining_a_calibre_only_job_adds_the_kindle(client):
    """Anca's share lands on a Calibre-only job that is still running."""
    first = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})
    second = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome",
                              "X-Deliver-To": "anca"})

    job = bs_main._download_jobs[first.json()["job_id"]]
    assert job["kindle_email"] == ANCA
    assert second.json()["message"] == "📖 Already on it: Obviously Awesome → Anca's Kindle"


def test_joining_after_the_kindle_step_does_not_rewrite_it(client):
    first = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})
    job = bs_main._download_jobs[first.json()["job_id"]]
    job["kindle_attempted"] = True

    share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome",
                     "X-Deliver-To": "anca"})

    assert not job["kindle_email"]


def test_a_finished_job_does_not_swallow_a_new_share(client):
    first = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})
    bs_main._download_jobs[first.json()["job_id"]]["finished"] = True

    second = share(client, **{"X-Book-Url": MD5, "X-Book-Title": "Obviously Awesome"})

    assert second.json()["job_id"] != first.json()["job_id"]
    assert second.json()["message"].startswith("📖 Queued")


def test_an_internal_error_still_answers_with_a_message(client, monkeypatch):
    def broken(shared):
        raise RuntimeError("regex engine on fire")

    monkeypatch.setattr(bs_main, "extract_md5", broken)

    r = share(client, **{"X-Book-Url": MD5})

    assert r.status_code == 200, "a 5xx body would be replaced by the error page"
    body = r.json()
    assert body["status"] == "failed"
    assert body["message"].startswith("⚠️")
    assert waited(client, body["job_id"]) == body["message"]
