"""Tests for the libgen.li direct ebook download path.

Anna's Archive stopped being a usable fetch route: its md5 page loads fine, but
the free /slow_download/ endpoint serves a challenge FlareSolverr cannot solve
(times out), and /fast_download/ needs paid membership. Every Stacks attempt
since 2026-05-01 failed ("Mirror randombook.org failed").

AA's own md5 page links libgen mirrors, and libgen.li serves the file with no
challenge — but only via a two-step flow: ads.php is a landing page carrying a
single-use `get.php?md5=..&key=..` link that must be fetched with the landing
page as Referer. Passing ads.php straight to a plain GET (what the old code did)
returns HTML, never a file. These tests pin that flow.
"""

import hashlib

import httpx
import pytest

from backend.libgen import LibGenScraper

MD5 = "b8eef1eb09cab009626eb5eebb0223f4"
# Real MOBI: PalmDB puts the book name at offset 0, so magic-byte sniffing on
# the first 4 bytes fails — "BOOKMOBI" lives at offset 60.
MOBI = b"Principles" + b"\x00" * 50 + b"BOOKMOBI" + b"\x00" * 2000
EPUB = b"PK\x03\x04" + b"\x00" * 2000

# A delivered file is now checked against the md5 that was asked for, so a
# happy-path fixture has to ask for the hash its own payload actually has.
# The failure-path tests below keep the arbitrary MD5: they never get far
# enough to hash anything.
MOBI_MD5 = hashlib.md5(MOBI).hexdigest()
EPUB_MD5 = hashlib.md5(EPUB).hexdigest()


def ads_html(md5: str, key: str = "WUKQ514O52MPM9X6") -> str:
    return (
        '<html><body><table><tr><td>'
        f'<a href="get.php?md5={md5}&key={key}">GET</a>'
        '</td></tr></table></body></html>'
    )


ADS_HTML = ads_html(MD5)


def _scraper(handler):
    s = LibGenScraper()
    s.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 follow_redirects=True)
    s._working_mirror = "https://libgen.li"
    return s


# --- link extraction (pure) ----------------------------------------------

def test_extract_get_link_finds_keyed_url():
    assert LibGenScraper._extract_get_link(ADS_HTML) == \
        f"get.php?md5={MD5}&key=WUKQ514O52MPM9X6"


def test_extract_get_link_returns_none_when_absent():
    assert LibGenScraper._extract_get_link("<html>no download here</html>") is None


def test_extract_get_link_ignores_unrelated_anchors():
    html = '<a href="/search.php?q=x">search</a><a href="get.php?md5=a&key=K">GET</a>'
    assert LibGenScraper._extract_get_link(html) == "get.php?md5=a&key=K"


# --- filename parsing (pure) ---------------------------------------------

def test_filename_from_disposition_strips_quotes_and_padding():
    # libgen.li really does emit a leading space inside the quotes.
    hdr = 'attachment; filename=" Ray Dalio - Principles - libgen.li.mobi"'
    assert LibGenScraper._filename_from_disposition(hdr) == \
        "Ray Dalio - Principles - libgen.li.mobi"


def test_filename_from_disposition_handles_unquoted():
    assert LibGenScraper._filename_from_disposition(
        "attachment; filename=book.epub") == "book.epub"


@pytest.mark.parametrize("hdr", [None, "", "attachment"])
def test_filename_from_disposition_none_when_missing(hdr):
    assert LibGenScraper._filename_from_disposition(hdr) is None


# --- ebook sniffing (pure) -----------------------------------------------

def test_mobi_accepted_despite_non_magic_first_bytes():
    assert LibGenScraper._looks_like_ebook(MOBI)


def test_epub_accepted():
    assert LibGenScraper._looks_like_ebook(EPUB)


@pytest.mark.parametrize("blob", [
    b"<!DOCTYPE html><html>challenge</html>" + b"x" * 5000,
    b"<html>error</html>",
    b"",
    b"tiny",
])
def test_html_and_stubs_rejected(blob):
    assert not LibGenScraper._looks_like_ebook(blob)


# --- end-to-end flow ------------------------------------------------------

@pytest.mark.asyncio
async def test_download_file_follows_ads_then_get_with_referer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "ads.php" in str(request.url):
            return httpx.Response(200, text=ads_html(MOBI_MD5))
        seen["referer"] = request.headers.get("referer")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=MOBI, headers={
            "content-disposition": 'attachment; filename=" Principles.mobi"'})

    data, name = await _scraper(handler).download_file(MOBI_MD5)

    assert data == MOBI
    assert name == "Principles.mobi"
    assert seen["url"] == f"https://libgen.li/get.php?md5={MOBI_MD5}&key=WUKQ514O52MPM9X6"
    # Referer is load-bearing — libgen.li rejects the keyed URL without it.
    assert seen["referer"] == f"https://libgen.li/ads.php?md5={MOBI_MD5}"


@pytest.mark.asyncio
async def test_download_file_synthesises_name_when_no_disposition():
    def handler(request):
        if "ads.php" in str(request.url):
            return httpx.Response(200, text=ads_html(EPUB_MD5))
        return httpx.Response(200, content=EPUB)

    data, name = await _scraper(handler).download_file(EPUB_MD5)
    assert data == EPUB
    assert name == f"{EPUB_MD5}.epub"


@pytest.mark.asyncio
async def test_download_file_returns_none_when_no_get_link():
    def handler(request):
        return httpx.Response(200, text="<html>nothing</html>")

    assert await _scraper(handler).download_file(MD5) == (None, None)


@pytest.mark.asyncio
async def test_download_file_rejects_html_masquerading_as_file():
    """A challenge/error page must not be written to the library as an ebook."""
    def handler(request):
        if "ads.php" in str(request.url):
            return httpx.Response(200, text=ADS_HTML)
        return httpx.Response(200, content=b"<!DOCTYPE html>" + b"x" * 5000)

    assert await _scraper(handler).download_file(MD5) == (None, None)


@pytest.mark.asyncio
async def test_download_file_survives_http_error():
    def handler(request):
        if "ads.php" in str(request.url):
            return httpx.Response(200, text=ADS_HTML)
        return httpx.Response(503, text="busy")

    assert await _scraper(handler).download_file(MD5) == (None, None)


@pytest.mark.asyncio
async def test_download_file_returns_none_when_ads_page_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom")

    assert await _scraper(handler).download_file(MD5) == (None, None)
