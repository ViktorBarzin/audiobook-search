"""Tests for audiobook duplicate detection.

Audiobookshelf ended up with two copies of "Principles for Dealing with the
Changing World Order" (2026-08-08). Both torrents were queued back-to-back, and
the old check only asked Audiobookshelf what it had *imported* — a book still
downloading was invisible, so both passed and both landed.

The old matcher was also unsafe in the other direction: it accepted a match when
either title contained the other, so "Principles" would have matched "Principles
for Success" and blocked a genuinely different Dalio book. A false positive is
worse than a duplicate — it silently refuses a download the user asked for.

So matching is deliberately conservative: same author, and either the full
normalised titles are equal, or one side has no subtitle and equals the other's
main title. Two titles that BOTH carry subtitles must match in full, which keeps
series entries ("Dune: Book One" vs "Dune: Book Two") apart.
"""

import pytest

from backend.dedupe import (
    find_duplicate,
    find_inflight_duplicate,
    is_same_book,
    normalize_author,
    normalize_title,
)


# --- normalize_title ------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Principles: Life and Work", "principles life and work"),
    ("Principles: Life and Work (Unabridged)", "principles life and work"),
    ("Thinking, Fast and Slow", "thinking fast and slow"),
    ("  Die  With   Zero  ", "die with zero"),
    ("A Court of Mist and Fury [Dramatized Adaptation]", "a court of mist and fury"),
    ("High Output Management (Audiobook)", "high output management"),
    ("Superforecasting — The Art and Science", "superforecasting the art and science"),
])
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_normalize_title_handles_none_and_empty():
    assert normalize_title(None) == ""
    assert normalize_title("") == ""


def test_normalize_author_drops_punctuation_and_case():
    assert normalize_author("Ray  Dalio;") == "ray dalio"
    assert normalize_author(None) == ""


# --- is_same_book: true positives ----------------------------------------

def test_edition_suffix_is_same_book():
    assert is_same_book("Principles: Life and Work", "Ray Dalio",
                        "Principles: Life and Work (Unabridged)", "Ray Dalio")


def test_punctuation_difference_is_same_book():
    assert is_same_book("Thinking, Fast and Slow", "Daniel Kahneman",
                        "Thinking Fast and Slow", "Daniel Kahneman")


def test_missing_subtitle_matches_main_title():
    # libgen/MAM often list the bare main title for the same book.
    assert is_same_book("Principles", "Ray Dalio",
                        "Principles: Life and Work", "Ray Dalio")


def test_author_formatting_difference_still_matches():
    assert is_same_book("High Output Management", "Andrew S Grove",
                        "High Output Management", "Andrew S. Grove")


def test_unknown_author_falls_back_to_title_match():
    assert is_same_book("Superforecasting", "Unknown Author",
                        "Superforecasting", "Philip E Tetlock")


# --- is_same_book: false positives that must NOT match --------------------

def test_different_book_sharing_a_prefix_is_not_a_duplicate():
    """The old `a in b or b in a` rule wrongly matched these."""
    assert not is_same_book("Principles", "Ray Dalio",
                            "Principles for Success", "Ray Dalio")


def test_series_entries_with_distinct_subtitles_are_not_duplicates():
    # Both carry subtitles, so a main-title match is not enough.
    assert not is_same_book("Dune: Book One", "Frank Herbert",
                            "Dune: Book Two", "Frank Herbert")


def test_same_title_different_author_is_not_a_duplicate():
    assert not is_same_book("Principles", "Ray Dalio",
                            "Principles", "Someone Else")


def test_changing_world_order_vs_big_debt_crises_are_distinct():
    assert not is_same_book(
        "Principles for Dealing with the Changing World Order", "Ray Dalio",
        "Principles for Navigating Big Debt Crises", "Ray Dalio")


def test_empty_title_never_matches():
    assert not is_same_book("", "Ray Dalio", "Principles", "Ray Dalio")


# --- find_inflight_duplicate ---------------------------------------------
# save_path is book-search's own canonical "/audiobooks/{author}/{title}", so it
# is far more reliable to match on than the torrent's free-form display name.

TORRENTS = [
    {"name": "Ray Dalio - Principles for Dealing with the Changing World Order [M4B]",
     "save_path": "/audiobooks/Ray Dalio/Principles for Dealing with the Changing World Order",
     "progress": 0.3},
    {"name": "Some Other Book",
     "save_path": "/audiobooks/Bill Perkins/Die with Zero", "progress": 1.0},
]


def test_inflight_duplicate_detected_from_save_path():
    hit = find_inflight_duplicate(
        "Principles for Dealing with the Changing World Order", "Ray Dalio", TORRENTS)
    assert hit is not None
    assert hit["progress"] == 0.3


def test_inflight_duplicate_matches_despite_edition_suffix():
    assert find_inflight_duplicate(
        "Principles for Dealing with the Changing World Order (Unabridged)",
        "Ray Dalio", TORRENTS) is not None


def test_inflight_returns_none_for_a_new_book():
    assert find_inflight_duplicate("Antifragile", "Nassim Nicholas Taleb",
                                   TORRENTS) is None


def test_inflight_does_not_match_different_book_by_same_author():
    assert find_inflight_duplicate("Principles for Navigating Big Debt Crises",
                                   "Ray Dalio", TORRENTS) is None


def test_inflight_ignores_torrents_without_a_usable_save_path():
    junk = [{"name": "x", "save_path": "/downloads", "progress": 0.1},
            {"name": "y", "progress": 0.1}]
    assert find_inflight_duplicate("Principles", "Ray Dalio", junk) is None


def test_inflight_handles_empty_list():
    assert find_inflight_duplicate("Principles", "Ray Dalio", []) is None


# --- find_duplicate (Calibre library rows) --------------------------------
# Ebook downloads previously only scanned the ingest FOLDER by filename
# substring and never asked Calibre what it already held. libgen names its file
# "Ray Dalio - Principles - libgen.li.mobi", which does not contain the string
# "Principles: Life and Work", so the check missed and a second Calibre entry
# was created (2026-08-08).

CALIBRE_ROWS = [
    ("Principles: Life and Work", "Ray Dalio"),
    ("Principles for Success", "Ray Dalio"),
    ("From Blood and Ash", "Jennifer L. Armentrout"),
    ("Thinking, Fast and Slow", "Daniel Kahneman"),
]


def test_calibre_duplicate_found_for_bare_main_title():
    """The exact miss that created the duplicate Calibre entry."""
    assert find_duplicate("Principles", "Ray Dalio", CALIBRE_ROWS) == \
        ("Principles: Life and Work", "Ray Dalio")


def test_calibre_duplicate_found_despite_punctuation():
    assert find_duplicate("Thinking Fast and Slow", "Daniel Kahneman",
                          CALIBRE_ROWS) is not None


def test_calibre_distinct_book_by_same_author_not_flagged():
    assert find_duplicate("Principles for Navigating Big Debt Crises",
                          "Ray Dalio", CALIBRE_ROWS) is None


def test_calibre_new_book_not_flagged():
    assert find_duplicate("Antifragile", "Nassim Nicholas Taleb",
                          CALIBRE_ROWS) is None


def test_calibre_same_title_other_author_not_flagged():
    assert find_duplicate("Principles: Life and Work", "Someone Else",
                          CALIBRE_ROWS) is None


def test_calibre_empty_library():
    assert find_duplicate("Principles", "Ray Dalio", []) is None
    assert find_duplicate("Principles", "Ray Dalio", None) is None
