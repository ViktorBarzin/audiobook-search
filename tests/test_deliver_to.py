"""Only the first attachment in an HTTP header dictionary resolves.

Measured live 2026-09-07 with both freshly installed shortcuts:

    download-url: url='' kindle_email=None title=None author=None page=263984b

Four headers are declared. X-Api-Key is first in the dictionary and arrives, so
auth passes and the request reaches the handler. X-Book-Url, X-Book-Title and
X-Kindle-Email all arrive empty, and that is regardless of what they reference:
the first two read a Safari page property, the last reads a Text action exactly
like the api key does. The page body arrives in full because it rides
WFRequestVariable rather than the dictionary.

That corrects what commit 67caa71 concluded. Removing the URL Encode step did
not make those values arrive; position in the dictionary is what decides.

So the shortcut stops asking iOS to resolve anything but the one attachment
that works. Who to email becomes a plain static string, different per variant,
and the address itself lives on the server where it can be changed without
reinstalling anything.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper

MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"
ANCA = "ancaelena98_4RMJsy@kindle.com"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(bs_main, "_process_download", noop)
    monkeypatch.setattr(bs_main, "_notify_slack", noop)

    async def no_detail(md5):
        return None

    monkeypatch.setattr(bs_main, "_detail_best_effort", no_detail)
    monkeypatch.setattr(bs_main, "annas_scraper", AnnasArchiveScraper())
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return TestClient(bs_main.app)


def hdrs(**extra):
    return {"X-Api-Key": "test-key", **extra}


def job_of(response):
    return bs_main._download_jobs[response.json()["job_id"]]


def test_a_named_recipient_becomes_an_address(client):
    r = client.post("/api/download-url",
                    headers=hdrs(**{"X-Book-Url": MD5, "X-Deliver-To": "anca"}))

    assert r.status_code == 200, r.text
    assert job_of(r)["kindle_email"] == ANCA


def test_no_recipient_means_no_email(client):
    """Viktor's variant imports and stops, because he has no Kindle address."""
    r = client.post("/api/download-url", headers=hdrs(**{"X-Book-Url": MD5}))

    assert r.status_code == 200, r.text
    assert not job_of(r)["kindle_email"]


def test_an_empty_recipient_means_no_email(client):
    r = client.post("/api/download-url",
                    headers=hdrs(**{"X-Book-Url": MD5, "X-Deliver-To": ""}))

    assert r.status_code == 200, r.text
    assert not job_of(r)["kindle_email"]


def test_an_unknown_recipient_is_ignored_rather_than_guessed(client):
    r = client.post("/api/download-url",
                    headers=hdrs(**{"X-Book-Url": MD5, "X-Deliver-To": "nobody"}))

    assert r.status_code == 200, r.text
    assert not job_of(r)["kindle_email"]


def test_an_explicit_address_still_wins(client):
    """The older callers that send a real address keep working."""
    r = client.post("/api/download-url",
                    headers=hdrs(**{"X-Book-Url": MD5,
                                    "X-Kindle-Email": "someone@kindle.com",
                                    "X-Deliver-To": "anca"}))

    assert r.status_code == 200, r.text
    assert job_of(r)["kindle_email"] == "someone@kindle.com"


def test_the_recipient_name_is_case_insensitive(client):
    r = client.post("/api/download-url",
                    headers=hdrs(**{"X-Book-Url": MD5, "X-Deliver-To": "Anca"}))

    assert r.status_code == 200, r.text
    assert job_of(r)["kindle_email"] == ANCA


def test_recipients_parse_from_the_env_shape():
    parse = bs_main._parse_recipients

    assert parse("anca:a@kindle.com") == {"anca": "a@kindle.com"}
    assert parse("anca:a@kindle.com,mum:b@kindle.com") == {
        "anca": "a@kindle.com", "mum": "b@kindle.com"}
    assert parse(" anca : a@kindle.com ") == {"anca": "a@kindle.com"}
    assert parse("") == {}
    assert parse("nonsense") == {}, "a pair with no colon is not a recipient"
