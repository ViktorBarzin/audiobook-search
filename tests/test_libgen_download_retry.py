"""The download itself must survive a dropped connection.

Live 2026-08-16: libgen closed the connection mid-transfer for So Good They
Can't Ignore You — 788,696 bytes of an expected 1,176,897. The search path
already retried; the download did not, so one blip failed the whole ingest.
The keyed get.php link is single-use, so a retry has to start again from ads.php.
"""

import httpx

from backend.libgen import LibGenScraper

ADS_PAGE = '<html><a href="get.php?md5=abc&key=KEY123">GET</a></html>'
EBOOK = b"PK\x03\x04" + b"x" * 40_000


def build_scraper(handler):
    scraper = LibGenScraper()
    scraper.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       follow_redirects=True)
    scraper._working_mirror = "https://libgen.li"
    return scraper


async def test_retries_after_a_dropped_connection():
    calls = {"ads": 0, "get": 0}

    def handler(request):
        if "ads.php" in request.url.path:
            calls["ads"] += 1
            return httpx.Response(200, text=ADS_PAGE)
        calls["get"] += 1
        if calls["get"] == 1:
            raise httpx.RemoteProtocolError("peer closed connection", request=request)
        return httpx.Response(200, content=EBOOK,
                              headers={"content-disposition": 'filename="book.epub"'})

    scraper = build_scraper(handler)
    data, filename = await scraper.download_file("abc")

    assert data == EBOOK
    assert filename == "book.epub"
    assert calls["ads"] == 2, "the keyed link is single-use, so ads.php is re-fetched"
    await scraper.close()


async def test_gives_up_after_repeated_failures():
    def handler(request):
        if "ads.php" in request.url.path:
            return httpx.Response(200, text=ADS_PAGE)
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    scraper = build_scraper(handler)
    data, filename = await scraper.download_file("abc")

    assert data is None and filename is None
    await scraper.close()


async def test_a_challenge_page_is_not_retried_into_acceptance():
    def handler(request):
        if "ads.php" in request.url.path:
            return httpx.Response(200, text=ADS_PAGE)
        return httpx.Response(200, content=b"<!DOCTYPE html><html>blocked</html>")

    scraper = build_scraper(handler)
    data, _ = await scraper.download_file("abc")

    assert data is None
    await scraper.close()
