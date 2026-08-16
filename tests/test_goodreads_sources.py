"""Tests for ISBN handling and the libgen result adapter.

Goodreads supplies ISBN-10 on most items, and libgen's identifier index only
answers to ISBN-13 — querying the raw ISBN-10 returns nothing at all. The
conversion is therefore load-bearing, not cosmetic.
"""

import httpx
import pytest

from backend.goodreads.isbn import to_isbn13
from backend.goodreads.sources import parse_size_bytes, rows_to_candidates
from backend.libgen import LibGenScraper


@pytest.mark.parametrize("isbn10,isbn13", [
    ("006343315X", "9780063433151"),   # Strange Houses, verified against libgen
    ("4925080814", "9784925080811"),   # The Tokyo Zodiac Murders, verified
    ("0-306-40615-2", "9780306406157"),
])
def test_converts_isbn10_to_isbn13(isbn10, isbn13):
    assert to_isbn13(isbn10) == isbn13


def test_passes_through_isbn13():
    assert to_isbn13("9780063433168") == "9780063433168"


@pytest.mark.parametrize("bad", ["", None, "not-an-isbn", "12345"])
def test_rejects_unusable_isbn(bad):
    assert to_isbn13(bad) is None


@pytest.mark.parametrize("text,expected", [
    ("661 kB", 661_000),
    ("1 MB", 1_000_000),
    ("4.2 MB", 4_200_000),
    ("512 B", 512),
    ("1 GB", 1_000_000_000),
    ("", None),
    ("unknown", None),
])
def test_parses_libgen_size_strings(text, expected):
    assert parse_size_bytes(text) == expected


# libgen.li serves a 9-column table; language sits at index 4 and the existing
# scraper ignored it, which is why non-English editions could be selected.
LIBGEN_HTML = """
<table class="table-striped">
<tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th><th>Language</th>
    <th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr>
<tr><td>Neuromancer</td><td>William Gibson</td><td>Ace</td><td>1984</td><td>English</td>
    <td>288</td><td>661 kB</td><td>epub</td>
    <td><a href="ads.php?md5=b0ba70d40e6f3edc41dd32b4b1b13646">1</a></td></tr>
<tr><td>Biochips</td><td>William Gibson</td><td>Heyne</td><td>1988</td><td>German</td>
    <td>93</td><td>1 MB</td><td>pdf</td>
    <td><a href="ads.php?md5=9257219221aaad7917fd1a00b685f7d0">1</a></td></tr>
</table>
"""


def test_rows_to_candidates_captures_language_and_size():
    candidates = rows_to_candidates(LIBGEN_HTML)

    assert len(candidates) == 2
    first, second = candidates
    assert first.title == "Neuromancer"
    assert first.author == "William Gibson"
    assert first.language == "English"
    assert first.ext == "epub"
    assert first.size_bytes == 661_000
    assert first.md5 == "b0ba70d40e6f3edc41dd32b4b1b13646"
    assert second.language == "German"


def test_rows_to_candidates_tolerates_no_table():
    assert rows_to_candidates("<html><body>nothing here</body></html>") == []


async def test_search_by_isbn_queries_isbn13_against_the_file_index():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text=LIBGEN_HTML)

    scraper = LibGenScraper()
    scraper.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scraper._working_mirror = "https://libgen.li"

    results = await scraper.search_by_isbn("006343315X")

    assert "9780063433151" in seen["url"], "must query the converted ISBN-13"
    assert "objects" in seen["url"]
    assert len(results) == 2
    await scraper.close()


async def test_search_by_isbn_skips_unusable_isbn():
    scraper = LibGenScraper()
    scraper.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    scraper._working_mirror = "https://libgen.li"

    assert await scraper.search_by_isbn("nope") == []
    await scraper.close()
