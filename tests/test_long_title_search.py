"""A long title and an Anna's Archive author qualifier find nothing on libgen.

Live 2026-09-07, sharing The Mom Test from the phone. The page parsed cleanly:

    Parsed the posted page for 14c3eabe...:
      'The Mom Test: how to talk to customers and learn if your business is a
       good idea when everybody is lying to you' by 'Rob Fitzpatrick, (Entrepreneur)'
    No confident libgen match for '...' by 'Rob Fitzpatrick, (Entrepreneur)' (0 candidates)

Zero candidates, while the same mirror returns five rows for "The Mom Test Rob
Fitzpatrick". Two things in that query defeat it: the whole subtitle, and the
occupation qualifier Anna's Archive appends to author names. The qualifier also
breaks matching, because the surname matcher reads the last token and gets
"(Entrepreneur)".

This is the same failure as the original one. The book is on libgen under other
hashes and the fallback could not find it. So the search net widens while the
identity test stays exactly as strict: candidates are still judged against the
full title and the real author name.
"""

import hashlib

import pytest

from backend.goodreads.matcher import Candidate

EBOOK = b"PK\x03\x04" + b"y" * 90_000
EBOOK_MD5 = hashlib.md5(EBOOK).hexdigest()

LONG_TITLE = ("The Mom Test: how to talk to customers and learn if your business "
              "is a good idea when everybody is lying to you")
AA_AUTHOR = "Rob Fitzpatrick, (Entrepreneur)"


class MirrorThatNeedsAShortQuery:
    """Returns rows only once the query stops carrying the subtitle."""

    def __init__(self, answers, downloadable):
        self.answers = answers
        self.downloadable = downloadable
        self.queries = []

    async def search_candidates(self, query):
        self.queries.append(query)
        return self.answers.get(query, [])

    async def download_file(self, md5):
        return (self.downloadable.get(md5), "The Mom Test.epub") if md5 in self.downloadable else (None, None)


def candidate(md5, title, author, ext="epub", size=1_500_000):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language="English", size_bytes=size, source="libgen")


@pytest.fixture
def fallback():
    from backend.main import _libgen_by_title
    return _libgen_by_title


async def test_a_subtitle_does_not_have_to_be_in_the_query(fallback, monkeypatch):
    row = candidate(EBOOK_MD5, LONG_TITLE + " v1.04 b l 1641747", "Rob Fitzpatrick")
    fake = MirrorThatNeedsAShortQuery(
        answers={"The Mom Test Rob Fitzpatrick": [row]},
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, filename = await fallback(LONG_TITLE, AA_AUTHOR)

    assert data == EBOOK, f"tried {fake.queries}"
    assert filename == "The Mom Test.epub"


async def test_the_full_query_is_still_tried_first(fallback, monkeypatch):
    """Nothing changes for the queries that already work."""
    fake = MirrorThatNeedsAShortQuery(answers={}, downloadable={})
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    await fallback("Obviously Awesome", "April Dunford")

    assert fake.queries[0] == "Obviously Awesome April Dunford"


async def test_a_mirror_that_answers_the_first_query_is_asked_once(fallback, monkeypatch):
    row = candidate(EBOOK_MD5, "Obviously Awesome", "April Dunford")
    fake = MirrorThatNeedsAShortQuery(
        answers={"Obviously Awesome April Dunford": [row]},
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    await fallback("Obviously Awesome", "April Dunford")

    assert fake.queries == ["Obviously Awesome April Dunford"], "no needless retries"


async def test_the_author_qualifier_does_not_decide_the_surname(fallback, monkeypatch):
    """"(Entrepreneur)" as the last token means no candidate can ever match."""
    row = candidate(EBOOK_MD5, "The Mom Test", "Rob Fitzpatrick")
    fake = MirrorThatNeedsAShortQuery(
        answers={"The Mom Test Rob Fitzpatrick": [row]},
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback("The Mom Test", AA_AUTHOR)

    assert data == EBOOK, f"tried {fake.queries}"


async def test_a_wider_search_does_not_loosen_identity(fallback, monkeypatch):
    """The whole point of the shorter query is more rows, not a weaker test.

    "Dune" must not accept "Dune Messiah" however the row was found.
    """
    row = candidate(EBOOK_MD5, "Dune Messiah", "Frank Herbert")
    fake = MirrorThatNeedsAShortQuery(
        answers={
            "Dune: the graphic novel Frank Herbert": [],
            "Dune Frank Herbert": [row],
            "Dune": [row],
        },
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback("Dune: the graphic novel", "Frank Herbert")

    assert data is None, "a different book must not be substituted"
