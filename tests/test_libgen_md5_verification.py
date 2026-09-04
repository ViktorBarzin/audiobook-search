"""A download that does not hash to the md5 we asked for is not the book.

Live 2026-09-04, fetching Obviously Awesome (April Dunford): libgen's
cdn3.booksdl.lc answered 200 and stopped at exactly 1,048,576 bytes of a
~2 MB epub, then 121,017 bytes on the next attempt. Both prefixes start
"PK\x03\x04", so the existing HTML-rejection check passed them and a corrupt
epub would have gone to Calibre and on to a Kindle.

The md5 is the thing we asked for, so it is also a free integrity check.
"""

import hashlib

import httpx

from backend.libgen import LibGenScraper

EBOOK = b"PK\x03\x04" + b"x" * 40_000
EBOOK_MD5 = hashlib.md5(EBOOK).hexdigest()
ADS_PAGE = f'<html><a href="get.php?md5={EBOOK_MD5}&key=KEY123">GET</a></html>'


def build_scraper(handler):
    scraper = LibGenScraper()
    scraper.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       follow_redirects=True)
    scraper._working_mirror = "https://libgen.li"
    return scraper


async def test_a_truncated_file_is_rejected_not_ingested():
    """The 1 MiB cut that started this: valid zip prefix, wrong hash."""
    def handler(request):
        if "ads.php" in request.url.path:
            return httpx.Response(200, text=ADS_PAGE)
        return httpx.Response(200, content=EBOOK[:20_000],
                              headers={"content-disposition": 'filename="book.epub"'})

    scraper = build_scraper(handler)
    data, filename = await scraper.download_file(EBOOK_MD5)

    assert data is None, "a prefix of the file must not pass as the file"
    assert filename is None
    await scraper.close()


async def test_a_complete_file_is_accepted():
    def handler(request):
        if "ads.php" in request.url.path:
            return httpx.Response(200, text=ADS_PAGE)
        return httpx.Response(200, content=EBOOK,
                              headers={"content-disposition": 'filename="book.epub"'})

    scraper = build_scraper(handler)
    data, filename = await scraper.download_file(EBOOK_MD5)

    assert data == EBOOK
    assert filename == "book.epub"
    await scraper.close()


async def test_a_truncated_attempt_is_retried_and_the_full_file_wins():
    """cdn3 truncated at a different offset each time, so a retry can succeed."""
    calls = {"get": 0}

    def handler(request):
        if "ads.php" in request.url.path:
            return httpx.Response(200, text=ADS_PAGE)
        calls["get"] += 1
        body = EBOOK[:20_000] if calls["get"] == 1 else EBOOK
        return httpx.Response(200, content=body,
                              headers={"content-disposition": 'filename="book.epub"'})

    scraper = build_scraper(handler)
    data, _ = await scraper.download_file(EBOOK_MD5)

    assert data == EBOOK
    assert calls["get"] == 2, "the short attempt should be retried, not accepted"
    await scraper.close()


async def test_an_identifier_that_is_not_an_md5_skips_the_hash_check():
    """download_file also serves callers holding a libgen id, not a hash.

    Those cannot be verified, so the check has to stay opt-in on the shape of
    the identifier rather than fail every such call.
    """
    def handler(request):
        if "ads.php" in request.url.path:
            return httpx.Response(200, text=ADS_PAGE)
        return httpx.Response(200, content=EBOOK,
                              headers={"content-disposition": 'filename="book.epub"'})

    scraper = build_scraper(handler)
    data, _ = await scraper.download_file("not-a-hash")

    assert data == EBOOK
    await scraper.close()
