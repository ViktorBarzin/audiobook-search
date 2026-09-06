"""The posted page alone is enough to identify the book.

Measured live 2026-09-06, second real run from the phone, this time with every
value in a header:

    POST /api/download-url  400 Bad Request
    download-url: url='' kindle_email=None title=None author=None page=263690b
    headers=['x-book-url', 'x-book-title', 'x-kindle-email'] body=265092b

So the header NAMES arrived and the page body arrived in full, but the URL and
title values were empty. The API key header was not empty, since auth passed.
The three that worked (api key, kindle address, page body) reference an action
output directly; the two that came back empty are the two that pass through a
URL Encode action first. The shortcut drops that step.

Independently of the shortcut, the server should not need the URL at all when
the page is right there: an Anna's Archive detail page names its own md5 in
every download link on it. That makes the flow survive any future variable that
fails to resolve, which is the failure mode this endpoint keeps hitting.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper

MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"
OTHER_MD5 = "aaaaaaaabbbbbbbbccccccccdddddddd"

# Shaped like the real detail page: the book's own md5 appears in several
# download links, while a "related book" is linked once.
AA_PAGE = f"""<!DOCTYPE html><html><head>
  <title>Obviously Awesome - Anna's Archive</title>
  <meta property="og:url" content="https://annas-archive.org/md5/{MD5}">
</head><body>
  <div class="text-3xl">Obviously Awesome</div>
  <div class="italic">April Dunford</div>
  <a href="/slow_download/{MD5}/0/0">Slow download</a>
  <a href="/fast_download/{MD5}/0/0">Fast download</a>
  <a href="https://libgen.li/ads.php?md5={MD5}">Libgen.li</a>
  <h3>Readers also enjoyed</h3>
  <a href="/md5/{OTHER_MD5}">Some Other Book</a>
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


def test_a_page_with_no_url_anywhere_still_identifies_the_book(client):
    """This is the exact live request that 400d: page body, empty headers."""
    r = client.post(
        "/api/download-url",
        content=AA_PAGE.encode(),
        headers=hdrs(**{
            "Content-Type": "text/html",
            "X-Book-Url": "",
            "X-Book-Title": "",
        }),
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Obviously Awesome"
    assert body["author"] == "April Dunford"
    assert bs_main._download_jobs[body["job_id"]]["md5"] == MD5


def test_a_related_book_link_does_not_win(client):
    """The page's own md5 repeats; a recommendation appears once."""
    r = client.post(
        "/api/download-url",
        content=AA_PAGE.encode(),
        headers=hdrs(**{"Content-Type": "text/html"}),
    )

    assert r.status_code == 200, r.text
    assert bs_main._download_jobs[r.json()["job_id"]]["md5"] == MD5


def test_a_url_header_still_wins_over_the_page(client):
    """When the shortcut does supply the URL, it is the caller's stated intent."""
    r = client.post(
        "/api/download-url",
        content=AA_PAGE.encode(),
        headers=hdrs(**{
            "Content-Type": "text/html",
            "X-Book-Url": f"https://annas-archive.pk/md5/{OTHER_MD5}",
        }),
    )

    assert r.status_code == 200, r.text
    assert bs_main._download_jobs[r.json()["job_id"]]["md5"] == OTHER_MD5


def test_a_page_with_no_md5_at_all_is_still_a_400(client):
    r = client.post(
        "/api/download-url",
        content=b"<!DOCTYPE html><html><body>Nothing book-shaped here.</body></html>",
        headers=hdrs(**{"Content-Type": "text/html"}),
    )

    assert r.status_code == 400


def test_a_raw_title_with_a_stray_percent_is_not_mangled(client):
    """Percent-decoding must only happen when the value is actually encoded.

    The shortcut no longer percent-encodes, so "100% Awesome" would otherwise
    hit unquote and lose characters.
    """
    r = client.post(
        "/api/download-url",
        headers=hdrs(**{"X-Book-Url": MD5, "X-Book-Title": "100% Awesome"}),
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "100% Awesome"


def test_a_utf8_title_sent_raw_survives_the_header(client):
    """iOS sends header bytes as UTF-8; the ASGI layer hands them over as latin-1."""
    title = "Смисълът на живота"
    r = client.post(
        "/api/download-url",
        headers=hdrs(**{
            "X-Book-Url": MD5,
            "X-Book-Title": title.encode("utf-8"),
        }),
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == title
