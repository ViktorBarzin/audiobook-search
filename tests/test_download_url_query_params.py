"""/api/download-url also reads query parameters.

The iOS Shortcut is generated rather than hand-built, and a generated shortcut
has to be right first time because there is no iOS instrument here to test it
on. Two of the three ways to carry data are risky to emit blind:

  - the JSON body key is documented inconsistently across references
    (WFJSONBody in one action library, WFJSONValues in another), and the wrong
    name sends an empty body with no error;
  - a dictionary field value is a nested WFDictionaryFieldValueItems structure.

A URL with query parameters is a plain WFTextTokenString, which is the
best-documented part of the format. So the shortcut puts url and title in the
query string and keeps only the API key in a header, and this endpoint reads
them. Body parsing is unchanged, so the existing shortcut keeps working.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")

    # Never start a real download from these tests. Both are handed to
    # asyncio.create_task, so both have to be coroutine functions.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(bs_main, "_process_download", noop)
    monkeypatch.setattr(bs_main, "_notify_slack", noop)

    async def no_detail(md5):
        return None

    monkeypatch.setattr(bs_main, "_detail_best_effort", no_detail)
    monkeypatch.setattr(bs_main, "annas_scraper", object())
    # The job store is module state and the endpoint dedupes on md5, so a job
    # left by an earlier test would be returned instead of a new one.
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return TestClient(bs_main.app)


MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"
HDRS = {"X-Api-Key": "test-key"}


def test_url_and_title_from_the_query_string(client):
    r = client.post(
        f"/api/download-url?url=https://annas-archive.gl/md5/{MD5}"
        "&title=Obviously%20Awesome%20-%20Anna%27s%20Archive",
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["title"] == "Obviously Awesome", "the AA suffix is stripped"


def test_a_bare_md5_in_the_query_string_works(client):
    r = client.post(f"/api/download-url?url={MD5}&title=Some%20Book", headers=HDRS)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Some Book"


def test_kindle_email_and_author_from_the_query_string(client):
    r = client.post(
        f"/api/download-url?url={MD5}&title=Book&author=A%20Writer"
        "&kindle_email=me%40kindle.com",
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["author"] == "A Writer"
    job = bs_main._download_jobs[r.json()["job_id"]]
    assert job["kindle_email"] == "me@kindle.com"


def test_a_json_body_still_wins_over_the_query_string(client):
    """The existing shortcut sends a body; it must keep working unchanged."""
    r = client.post(
        f"/api/download-url?url={MD5}&title=From%20Query",
        json={"url": MD5, "title": "From Body"},
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "From Body"


def test_no_url_anywhere_is_still_a_400(client):
    r = client.post("/api/download-url", headers=HDRS)
    assert r.status_code == 400


def test_the_api_key_is_still_required(client):
    r = client.post(f"/api/download-url?url={MD5}")
    assert r.status_code == 401
