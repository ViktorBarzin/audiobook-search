"""Anna's Archive via the cluster's headful Chrome.

AA refuses our HTTP clients outright: DDoS-Guard validates the TLS/HTTP
fingerprint, so even a human-solved session replayed through httpx returns 403.
It does serve the shared cluster browser once a human has passed the captcha
there, which is the only route that works — so search runs through that browser
over CDP, and the parsing below is kept pure so it can be tested without one.
"""

import pytest

from backend.goodreads.annas_source import AnnasSource, parse_rows
from backend.goodreads.sources import SourceUnavailable

ROW = {
    "md5": "3889af3fd3f3ffaf16d059a9b974f823",
    "lines": [
        "upload/bibliotik/T/The Tokyo Zodiac Murders - Soji Shimada.epub",
        "The Tokyo Zodiac Murders (Pushkin Vertigo Book 4)",
        "Soji Shimada, Ross Mackenzie, Shika Mackenzie",
        "Steerforth Press, London, 2015",
    ],
}


def test_parses_a_real_result_row():
    got = parse_rows([ROW])

    assert len(got) == 1
    c = got[0]
    assert c.md5 == "3889af3fd3f3ffaf16d059a9b974f823"
    assert c.title == "The Tokyo Zodiac Murders (Pushkin Vertigo Book 4)"
    assert c.author.startswith("Soji Shimada")
    assert c.ext == "epub"
    assert c.source == "annas"
    # The query pins lang=en; AA's rows carry no per-result language.
    assert c.language == "English"


def test_reads_the_extension_from_the_file_path_line():
    row = dict(ROW, lines=["upload/x/Book - Author.azw3", "Book", "Author"])
    assert parse_rows([row])[0].ext == "azw3"


def test_defaults_to_epub_when_the_path_says_nothing():
    row = dict(ROW, lines=["some text with no extension", "Book", "Author"])
    assert parse_rows([row])[0].ext == "epub"


def test_skips_rows_without_a_usable_md5():
    assert parse_rows([{"md5": "nope", "lines": ["x", "y"]}]) == []


def test_skips_rows_with_no_title():
    assert parse_rows([{"md5": "a" * 32, "lines": []}]) == []


def test_sidebar_links_survive_parsing_and_are_left_to_the_matcher():
    """Sidebar anchors parse into candidates; the strict matcher rejects them."""
    row = {"md5": "b" * 32, "lines": ["", "Kickstart PLC Programming", "Someone Else"]}
    got = parse_rows([row])
    assert len(got) == 1 and got[0].title == "Kickstart PLC Programming"


# --------------------------------------------------------------------------- #
# The browser session is a shared, human-maintained resource                   #
# --------------------------------------------------------------------------- #

async def test_a_challenged_page_is_an_outage_not_an_empty_result():
    """When the solved session lapses, AA must not look like 'book not found'."""
    async def evaluator(url, js):
        return {"title": "DDoS-Guard", "rows": []}

    src = AnnasSource(evaluator=evaluator)
    with pytest.raises(SourceUnavailable, match="captcha"):
        await src.search_candidates("neuromancer")


async def test_an_unreachable_browser_is_an_outage():
    async def evaluator(url, js):
        raise OSError("connection refused")

    src = AnnasSource(evaluator=evaluator)
    with pytest.raises(SourceUnavailable):
        await src.search_candidates("neuromancer")


async def test_a_good_page_returns_candidates():
    async def evaluator(url, js):
        assert "annas-archive" in url and "neuromancer" in url
        return {"title": "neuromancer - Search - Anna’s Archive", "rows": [ROW]}

    got = await AnnasSource(evaluator=evaluator).search_candidates("neuromancer")
    assert [c.md5 for c in got] == [ROW["md5"]]


async def test_isbn_search_converts_to_isbn13():
    seen = {}

    async def evaluator(url, js):
        seen["url"] = url
        return {"title": "Search - Anna’s Archive", "rows": []}

    await AnnasSource(evaluator=evaluator).search_by_isbn("006343315X")
    assert "9780063433151" in seen["url"]
