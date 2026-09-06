"""The shortcut sends its values as headers, not as inline text variables.

Measured live 2026-09-06, first real run from the phone:

    POST /api/download-url?url=&title=&kindle_email=   400 Bad Request
    download-url: url='' kindle_email=None title=None author=None

Every value arrived empty, four times. The API key did NOT: it reached the
handler, so auth passed. The difference between the two is the construct:

  - the key is a WFDictionaryFieldValue item whose WFValue is a whole
    WFTextTokenAttachment (works);
  - the url and title were inline attachments inside a WFTextTokenString,
    described by attachmentsByRange (resolved to nothing).

So the generated shortcut stops building strings with embedded variables. The
URL becomes a static string and every value rides in a header, which is the
shape already proven to work. Values are percent-encoded by the shortcut so a
title with spaces or non-ASCII cannot break the header.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper

MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"
AA_URL = f"https://annas-archive.gl/md5/{MD5}"

AA_PAGE = f"""<html><head>
  <title>Obviously Awesome - Anna's Archive</title>
</head><body>
  <div class="text-3xl">Obviously Awesome</div>
  <div class="italic">April Dunford</div>
  <a href="https://libgen.li/ads.php?md5={MD5}">Libgen.li</a>
</body></html>"""


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")

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


def test_url_and_title_arrive_as_percent_encoded_headers(client):
    r = client.post(
        "/api/download-url",
        headers=hdrs(**{
            "X-Book-Url": "https%3A%2F%2Fannas-archive.gl%2Fmd5%2F" + MD5,
            "X-Book-Title": "Obviously%20Awesome%20-%20Anna%27s%20Archive",
        }),
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Obviously Awesome", "decoded and AA suffix stripped"


def test_the_page_body_still_supplies_the_author(client):
    r = client.post(
        "/api/download-url",
        content=AA_PAGE.encode(),
        headers=hdrs(**{
            "Content-Type": "text/plain",
            "X-Book-Url": AA_URL,
            "X-Book-Title": "Obviously%20Awesome",
        }),
    )

    assert r.status_code == 200, r.text
    assert r.json()["author"] == "April Dunford"


def test_a_kindle_address_header_is_honoured(client):
    r = client.post(
        "/api/download-url",
        headers=hdrs(**{
            "X-Book-Url": MD5,
            "X-Book-Title": "Some%20Book",
            "X-Kindle-Email": "me%40kindle.com",
        }),
    )

    assert r.status_code == 200, r.text
    job = bs_main._download_jobs[r.json()["job_id"]]
    assert job["kindle_email"] == "me@kindle.com"


def test_an_unencoded_header_value_still_works(client):
    """Percent-decoding a plain string must be a no-op, not a mangling."""
    r = client.post(
        "/api/download-url",
        headers=hdrs(**{"X-Book-Url": MD5, "X-Book-Title": "Dune"}),
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Dune"


def test_an_empty_header_does_not_shadow_the_query_string(client):
    """The header is only used when it carries something."""
    r = client.post(
        f"/api/download-url?url={MD5}&title=From%20Query",
        headers=hdrs(**{"X-Book-Url": "", "X-Book-Title": ""}),
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "From Query"


def test_a_request_with_nothing_usable_says_what_arrived(client, caplog):
    """The first live failure logged url='' and nothing else, which said little."""
    import logging

    caplog.set_level(logging.INFO)
    r = client.post("/api/download-url?url=&title=", headers=hdrs())

    assert r.status_code == 400
    logged = " ".join(rec.message for rec in caplog.records)
    assert "query=" in logged or "headers=" in logged, (
        "a 400 should record what actually arrived, so the next one is diagnosable"
    )
