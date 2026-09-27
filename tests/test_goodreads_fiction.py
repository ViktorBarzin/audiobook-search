"""The pipeline searches libgen's fiction collection too, and the matcher keeps
the two rules that rejected correct novels from doing it again.

Found 2026-09-27: every book Anca shelved between 2026-09-08 and 2026-09-27
missed. Six of the seven English ones were on libgen all along, in the fiction
collection that `topics[]=l` excluded.
"""

from datetime import datetime, timezone

import pytest

from backend.goodreads.feed import parse_items
from backend.goodreads.matcher import Candidate, ShelfItem, rank_candidates, select_candidate
from backend.goodreads.sources import rows_to_candidates
from backend.libgen import LibGenScraper


def item(title, author, isbn=None):
    return ShelfItem(book_id="1", title=title, author=author, isbn=isbn,
                     added_at=datetime(2026, 9, 18, tzinfo=timezone.utc))


def offered(title, author, language="English", ext="epub", md5="a" * 32, size=800_000):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language=language, size_bytes=size, source="libgen")


# --------------------------------------------------------------------------- #
# Search covers both collections                                               #
# --------------------------------------------------------------------------- #

class RecordingClient:
    def __init__(self):
        self.params = []

    async def get(self, url, params=None, timeout=None):
        self.params.append(params)

        class Response:
            text = "<html></html>"

            def raise_for_status(self):
                return None

        return Response()


async def test_the_text_search_asks_for_fiction_and_non_fiction():
    scraper = LibGenScraper()
    scraper.client = RecordingClient()
    scraper._working_mirror = "https://libgen.li"

    await scraper.search_candidates("the sword of kaigen wang")

    topics = scraper.client.params[0]["topics[]"]
    assert sorted(topics) == ["f", "l"]


# --------------------------------------------------------------------------- #
# Rows carry publisher and year, which the Claude check reads                  #
# --------------------------------------------------------------------------- #

ROW_HTML = """
<table class="table table-striped" id="tablelibgen">
<tr><th>ID</th><th>Author(s)</th><th>Publisher</th><th>Year</th><th>Language</th>
<th>Pages</th><th>Size</th><th>Ext.</th><th>Mirrors</th></tr>
<tr>
<td>The Sword of Kaigen: A Theonite War Story b f 2498</td>
<td>M. L. Wang,</td><td>Amazon.com Services LLC</td><td>2019</td><td>English</td>
<td>651</td><td>714 kB</td><td>epub</td>
<td><a href="/ads.php?md5=cb82f21a8289092f534340a90ded54f3">1</a></td>
</tr>
</table>
"""


def test_a_row_keeps_its_publisher_and_year():
    [row] = rows_to_candidates(ROW_HTML)

    assert row.publisher == "Amazon.com Services LLC"
    assert row.year == "2019"
    assert row.md5 == "cb82f21a8289092f534340a90ded54f3"


def test_a_fiction_row_matches_the_shelved_novel():
    [row] = rows_to_candidates(ROW_HTML)

    match = select_candidate(item("The Sword of Kaigen", "M.L. Wang"), [row])

    assert match.candidate is row
    assert match.reason == "title_author"


# --------------------------------------------------------------------------- #
# An ISBN-10 ending in X is bookkeeping, not a title word                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("libgen_title", [
    "Release Me 006341905X",
    "Release Me 006341905x; 9780063419056",
])
def test_an_isbn10_with_a_check_letter_does_not_break_the_title(libgen_title):
    wanted = item("Release Me (Shatter Me: Series Two, #2)", "Tahereh Mafi")

    match = select_candidate(wanted, [offered(libgen_title, "Tahereh Mafi")])

    assert match.reason == "title_author"


def test_a_real_word_after_the_title_still_means_a_different_book():
    wanted = item("Release Me", "Tahereh Mafi")

    match = select_candidate(wanted, [offered("Release Me Novella 006341905X", "Tahereh Mafi")])

    assert match.candidate is None


# --------------------------------------------------------------------------- #
# A blank language is unknown, not foreign                                     #
# --------------------------------------------------------------------------- #

def test_a_blank_language_passes_on_a_full_title_and_author_match():
    wanted = item("The Midnight Train (The Midnight World, #2)", "Matt Haig")
    row = offered("The Midnight Train", "Matt Haig", language=None)

    match = select_candidate(wanted, [row])

    assert match.candidate is row


def test_a_blank_language_is_not_accepted_on_isbn_alone():
    """The ISBN tier skips title reasoning, so it keeps requiring English."""
    wanted = item("The Midnight Train", "Matt Haig", isbn="9780593833377")
    row = offered("Some Other Title", "Someone Else", language="")

    match = select_candidate(wanted, [row], isbn_matched_md5s={row.md5})

    assert match.candidate is None


def test_an_explicit_english_file_still_beats_an_unknown_one():
    wanted = item("The Midnight Train", "Matt Haig")
    unknown = offered("The Midnight Train", "Matt Haig", language=None, md5="b" * 32,
                      size=2_000_000)
    english = offered("The Midnight Train", "Matt Haig", md5="c" * 32, size=500_000)

    ranked = rank_candidates(wanted, [unknown, english])

    assert [c.md5 for c, _ in ranked] == [english.md5, unknown.md5]


def test_no_english_edition_needs_a_named_foreign_language():
    """A blank-language pdf by someone else used to be reported as 'no English
    edition', which sent the reader looking for the wrong problem."""
    wanted = item("The Midnight Train", "Matt Haig")
    row = offered("Train Timetables 1998", "Rail Board", language="", ext="pdf")

    match = select_candidate(wanted, [row])

    assert match.reason == "no_confident_match"


def test_a_named_foreign_language_is_still_refused():
    wanted = item("The Viceroys", "Federico De Roberto")
    row = offered("The Viceroys", "Federico De Roberto", language="Italian")

    match = select_candidate(wanted, [row])

    assert match.candidate is None
    assert match.reason == "no_english_edition"


# --------------------------------------------------------------------------- #
# Ranking returns every confident candidate, best first                        #
# --------------------------------------------------------------------------- #

def test_ranking_lists_every_confident_candidate_best_first():
    wanted = item("The Sword of Kaigen", "M.L. Wang", isbn="9781720191743")
    by_isbn = offered("The Sword of Kaigen", "Wang, M. L.", ext="mobi", md5="1" * 32)
    epub = offered("The Sword of Kaigen", "M. L. Wang", md5="2" * 32, size=700_000)
    bigger = offered("The Sword of Kaigen", "M.L. Wang", md5="3" * 32, size=900_000)
    unrelated = offered("Kaigen Cookbook", "Someone", md5="4" * 32)

    ranked = rank_candidates(wanted, [epub, unrelated, bigger, by_isbn],
                             isbn_matched_md5s={by_isbn.md5})

    assert [(c.md5, why) for c, why in ranked] == [
        (by_isbn.md5, "isbn"),
        (bigger.md5, "title_author"),
        (epub.md5, "title_author"),
    ]


# --------------------------------------------------------------------------- #
# The feed carries what the Claude check compares against                      #
# --------------------------------------------------------------------------- #

FEED_ITEM = """
<item>
  <title>Romanov</title>
  <book_id>40024133</book_id>
  <author_name>Nadine Brandes</author_name>
  <isbn>0785217240</isbn>
  <user_date_added><![CDATA[Thu, 18 Sep 2026 07:12:00 -0700]]></user_date_added>
  <book_published>2019</book_published>
  <book_description><![CDATA[The history books say I died. <br/>They don't know the half of it.]]></book_description>
</item>
"""


def test_a_feed_item_keeps_its_description_and_publication_year():
    [parsed] = parse_items(FEED_ITEM)

    assert parsed.published == "2019"
    assert "The history books say I died." in parsed.description
    assert "<br" not in parsed.description
