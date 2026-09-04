"""A title straight off the iOS share sheet has to be usable as-is.

Anna's Archive is human-only for us (every automated route measured 2026-09-04:
DDoS-Guard 403s /md5/ for curl, for a real Chrome TLS handshake via
curl-impersonate, and for the cluster's headful browser). So the title cannot
come from AA's page server-side. It comes from the phone, which can read AA.

AA's own page template is:

    <title>{% if self.title() %}{% block title %}{% endblock %} - {% endif %}
    {{ gettext('layout.index.title') }}</title>

so the share sheet hands over "Obviously Awesome - Anna's Archive". The server
strips the site suffix rather than asking anyone to do string surgery in
Shortcuts.

The page carries no author, so matching has to work from a title alone. That is
only safe when the title matches EXACTLY and every candidate offering it agrees
on one author, which is what keeps this from repeating the wrong-book bug where
a CISSP study guide got shelved as Neuromancer.
"""

import hashlib

import pytest

from backend.goodreads.matcher import Candidate

EBOOK = b"PK\x03\x04" + b"x" * 40_000
EBOOK_MD5 = hashlib.md5(EBOOK).hexdigest()
OTHER_MD5 = "b" * 32


class FakeLibGen:
    def __init__(self, candidates, downloadable=None):
        self.candidates = candidates
        self.downloadable = downloadable if downloadable is not None else {EBOOK_MD5: EBOOK}
        self.queries = []
        self.download_calls = []

    async def download_file(self, md5):
        self.download_calls.append(md5)
        if md5 in self.downloadable:
            return self.downloadable[md5], "book.epub"
        return None, None

    async def search_candidates(self, query):
        self.queries.append(query)
        return self.candidates


def cand(md5, title, author, ext="epub", size=2_000_000):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language="English", size_bytes=size, source="libgen")


# --- stripping AA's site suffix -------------------------------------------


@pytest.mark.parametrize("shared,expected", [
    ("Obviously Awesome - Anna's Archive", "Obviously Awesome"),
    ("Obviously Awesome - Anna’s Archive", "Obviously Awesome"),
    ("Dune 🔍 - Anna's Archive", "Dune"),
    ("Neuromancer - Annas Archive", "Neuromancer"),
    ("Obviously Awesome", "Obviously Awesome"),
    ("  Padded Title - Anna's Archive  ", "Padded Title"),
    # A hyphenated title must survive: only the AA suffix goes.
    ("Cost-Benefit Analysis - Anna's Archive", "Cost-Benefit Analysis"),
    ("Anna's Archive", ""),
    ("", ""),
    (None, ""),
])
def test_clean_shared_title(shared, expected):
    from backend.main import _clean_shared_title
    assert _clean_shared_title(shared) == expected


# --- title-only matching, and its guard -----------------------------------


async def test_a_title_alone_is_enough_when_the_author_is_unambiguous(monkeypatch):
    import backend.main as bs_main
    fake = FakeLibGen([
        cand(EBOOK_MD5, "Obviously Awesome", "April Dunford"),
        cand(OTHER_MD5, "Obviously Awesome", "Dunford, April"),
    ])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Obviously Awesome", "")

    assert data == EBOOK, "one author across the exact-title matches is safe"


async def test_two_different_authors_for_one_title_is_refused(monkeypatch):
    """'Principles' is Ray Dalio's and also several other books."""
    import backend.main as bs_main
    fake = FakeLibGen([
        cand(EBOOK_MD5, "Principles", "Ray Dalio"),
        cand(OTHER_MD5, "Principles", "Rudolf Carnap"),
    ])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Principles", "")

    assert data is None, "an ambiguous title must not pick one at random"
    assert fake.download_calls == []


async def test_a_near_miss_title_is_not_accepted_without_an_author(monkeypatch):
    """Without an author, only an EXACT title match counts."""
    import backend.main as bs_main
    fake = FakeLibGen([cand(EBOOK_MD5, "Dune Messiah", "Frank Herbert")])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Dune", "")

    assert data is None
    assert fake.download_calls == []


async def test_an_author_when_supplied_still_takes_the_matcher_path(monkeypatch):
    """The Goodreads matcher stays in charge whenever an author is known."""
    import backend.main as bs_main
    fake = FakeLibGen([cand(EBOOK_MD5, "Obviously Awesome", "April Dunford")])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Obviously Awesome", "April Dunford")

    assert data == EBOOK


async def test_the_aa_suffix_is_stripped_before_searching(monkeypatch):
    """The raw share-sheet string must not reach libgen's search box."""
    import backend.main as bs_main
    fake = FakeLibGen([cand(EBOOK_MD5, "Obviously Awesome", "April Dunford")])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    await bs_main._libgen_by_title("Obviously Awesome - Anna's Archive", "")

    assert fake.queries == ["Obviously Awesome"]


async def test_a_bare_site_title_is_not_searched_for(monkeypatch):
    """Sharing from AA's homepage yields just 'Anna's Archive'."""
    import backend.main as bs_main
    fake = FakeLibGen([])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Anna's Archive", "")

    assert data is None
    assert fake.queries == []


# --- real libgen title cells, which are not clean ------------------------


async def test_edition_and_isbn_noise_in_the_title_cell_still_matches(monkeypatch):
    """Measured against live libgen on 2026-09-04.

    Searching 'Thinking, Fast and Slow' returned 25 candidates and NOT ONE had
    a clean title: libgen's title cell absorbs edition wording and ISBNs, e.g.
    'Thinking, Fast and Slow 1st ed 9780141033570' and 'Thinking Fast And Slow
    b l 4322674'. Requiring exact equality refused every one of them, so the
    comparison has to be the matcher's noise-tolerant one.
    """
    import backend.main as bs_main
    fake = FakeLibGen([
        cand(EBOOK_MD5, "Thinking, Fast and Slow 1st ed 9780141033570", "Daniel Kahneman"),
        cand(OTHER_MD5, "Thinking Fast And Slow b l 4322674", "Kahneman, Daniel"),
    ])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Thinking, Fast and Slow", "")

    assert data == EBOOK


async def test_a_real_word_in_the_remainder_is_still_a_different_book(monkeypatch):
    """'Dune' must not take 'Dune Messiah', noise tolerance or not."""
    import backend.main as bs_main
    fake = FakeLibGen([cand(EBOOK_MD5, "Dune Messiah", "Frank Herbert")])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Dune", "")

    assert data is None
    assert fake.download_calls == []


async def test_noise_tolerance_does_not_defeat_the_ambiguity_guard(monkeypatch):
    """Two authors behind noisy spellings of one title is still ambiguous."""
    import backend.main as bs_main
    fake = FakeLibGen([
        cand(EBOOK_MD5, "Principles 1st ed 9781501124020", "Ray Dalio"),
        cand(OTHER_MD5, "Principles 2nd ed 0415288967", "Rudolf Carnap"),
    ])
    monkeypatch.setattr(bs_main, "libgen_scraper", fake)

    data, _ = await bs_main._libgen_by_title("Principles", "")

    assert data is None
