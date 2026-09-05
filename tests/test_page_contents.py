"""The phone sends the Anna's Archive page; the server parses it.

Anna's Archive is human-only for us. Measured 2026-09-04: DDoS-Guard 403s
/md5/ for plain requests, for six real browser TLS handshakes via
curl-impersonate, and for the cluster's headful Chrome on both a clean pool
worker and the neko master profile. The site root answers 200 throughout, so
it is the gated path rather than our address.

The phone can read AA, because a person is holding it. So the shortcut sends
the page Safari already rendered, and every decision made from it lives here in
Python, where it can be changed and deployed without anyone touching their
phone. That is the whole point: the shortcut is a dumb pipe, deliberately
generous, so it never needs editing again.

The author is the prize. Without it the libgen fallback has to match on title
alone, which refuses anything ambiguous. With it, the existing Goodreads
matcher can require title AND author to agree.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper

MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"

# Shaped after Anna's Archive's own markup: the title sits in a text-3xl div,
# the author in an italic div, and the page links its mirrors. Their page
# template builds <title> as "<page title> - <site title>".
AA_PAGE = f"""
<html><head>
  <title>Obviously Awesome - Anna's Archive</title>
  <meta property="og:title" content="Obviously Awesome">
</head><body>
  <div class="text-3xl">Obviously Awesome: How to Nail Product Positioning</div>
  <div class="italic">April Dunford</div>
  <div class="text-sm">English [en], epub, 2.1MB, Ambient Press, 2019</div>
  <a href="/slow_download/{MD5}/0/0">Slow partner server #1</a>
  <a href="https://libgen.li/ads.php?md5={MD5}">Libgen.li</a>
</body></html>
"""


# --- the parser, split from the fetch --------------------------------------


def test_parse_detail_reads_title_and_author_from_html():
    scraper = AnnasArchiveScraper()
    detail = scraper.parse_detail(AA_PAGE, MD5)

    assert detail is not None
    assert "Obviously Awesome" in detail.title
    assert detail.author == "April Dunford"


def test_parse_detail_collects_the_mirrors():
    scraper = AnnasArchiveScraper()
    detail = scraper.parse_detail(AA_PAGE, MD5)

    assert any("libgen.li" in u for u in detail.mirror_urls)
    assert any("/slow_download/" in u for u in detail.mirror_urls)


def test_parse_detail_survives_rubbish():
    scraper = AnnasArchiveScraper()
    assert scraper.parse_detail("", MD5) is None
    assert scraper.parse_detail("<html><body>nope</body></html>", MD5) is not None


# --- the endpoint accepting a page ----------------------------------------


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


HDRS = {"X-Api-Key": "test-key"}


def test_a_posted_page_supplies_title_and_author(client):
    r = client.post(
        "/api/download-url",
        json={"url": MD5, "page": AA_PAGE},
        headers=HDRS,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert "Obviously Awesome" in body["title"]
    assert body["author"] == "April Dunford", "the author is what the page is for"


def test_the_page_beats_a_share_sheet_title(client):
    """The page carries the author; the share sheet title never does."""
    r = client.post(
        "/api/download-url",
        json={"url": MD5, "title": "Obviously Awesome - Anna's Archive", "page": AA_PAGE},
        headers=HDRS,
    )

    assert r.status_code == 200, r.text
    assert r.json()["author"] == "April Dunford"


def test_a_page_that_parses_to_nothing_falls_back_to_the_title(client):
    r = client.post(
        "/api/download-url",
        json={
            "url": MD5,
            "title": "Obviously Awesome - Anna's Archive",
            "page": "<html><body>blocked</body></html>",
        },
        headers=HDRS,
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Obviously Awesome", "AA suffix still stripped"


def test_an_oversized_page_is_refused_rather_than_parsed(client):
    """A phone could send anything; parsing megabytes of it helps nobody."""
    r = client.post(
        "/api/download-url",
        json={"url": MD5, "title": "Some Book", "page": "x" * (bs_main.MAX_PAGE_BYTES + 1)},
        headers=HDRS,
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Some Book", "it still works, just without the page"


def test_no_page_still_works(client):
    """The existing shortcut sends no page and must keep working."""
    r = client.post(
        "/api/download-url",
        json={"url": MD5, "title": "Some Book"},
        headers=HDRS,
    )

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Some Book"


# --- a page must not divert the download away from what works --------------


class OnlyHashWorks:
    """libgen by hash succeeds; nothing else does."""

    def __init__(self, data=b"PK\x03\x04" + b"x" * 50_000):
        self.data = data
        self.hash_calls = []

    async def download_file(self, md5):
        self.hash_calls.append(md5)
        return self.data, "book.epub"

    async def search_candidates(self, query):
        return []


async def test_libgen_by_hash_is_tried_even_when_a_page_supplied_a_detail(monkeypatch):
    """The hash route is the one that actually delivers.

    Anna's Archive mirrors are /slow_download/ links we cannot use, so a parsed
    page must not push the job onto them and skip the route that works. Before
    this, _try_direct_download gated the hash attempt behind `not detail`.
    """
    libgen = OnlyHashWorks()
    uploaded = {}

    async def fake_upload(data, filename):
        uploaded["n"] = len(data)
        return 900

    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    monkeypatch.setattr(bs_main, "_upload_to_calibre", fake_upload)

    detail = AnnasArchiveScraper().parse_detail(AA_PAGE, MD5)
    assert detail is not None and detail.mirror_urls

    job = {}
    ok = await bs_main._try_direct_download(
        "j-page", job, MD5, "Obviously Awesome", "April Dunford", detail,
    )

    assert ok is True
    assert libgen.hash_calls == [MD5], "the hash route must run first, detail or not"
    assert job["book_id"] == 900
