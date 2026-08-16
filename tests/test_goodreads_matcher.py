"""Matcher tests.

The cases below are not invented: every rejection case was produced by a naive
title+author matcher run against Anca's real to-read shelf on 2026-08-16. With no
review step in the pipeline, these rules are the only thing between her shelf and
a wrong book, so they are pinned here.
"""

import pytest

from backend.goodreads.matcher import Candidate, ShelfItem, select_candidate


def item(title, author, isbn=None):
    return ShelfItem(book_id="1", title=title, author=author, isbn=isbn, added_at=None)


def cand(title, author, ext="epub", language="English", md5=None, size_bytes=500_000):
    return Candidate(
        md5=md5 or ("a" * 32),
        title=title,
        author=author,
        ext=ext,
        language=language,
        size_bytes=size_bytes,
        source="libgen",
    )


# --------------------------------------------------------------------------- #
# Rejections observed in the wild                                             #
# --------------------------------------------------------------------------- #

def test_rejects_unrelated_journal_for_placeholder_title():
    """'Untitled (ACOTAR #6)' matched 'The Journal of Roman Studies' naively."""
    result = select_candidate(
        item("Untitled (A Court of Thorns and Roses, #6)", "Sarah J. Maas"),
        [cand("The Journal of Roman Studies", "Sarah J. Maas")],
    )
    assert result.candidate is None
    assert result.reason == "placeholder_title"


def test_rejects_chemistry_journal_for_one_word_title():
    """'Malachite' matched 'Analytica Chimica Acta' naively."""
    result = select_candidate(
        item("Malachite", "Ashley Andersen"),
        [cand("Analytica Chimica Acta", "Ashley Andersen")],
    )
    assert result.candidate is None
    assert result.reason == "no_confident_match"


def test_rejects_html_fragment_rows():
    """Parser noise like 'l3cd8e0" href=edition.php?id=' must never match."""
    result = select_candidate(
        item("This Immortal Heart", "Jennifer Saint"),
        [cand('l3cd8e0" href="edition.php?id=1416121', "Jennifer Saint")],
    )
    assert result.candidate is None
    assert result.reason == "no_confident_match"


def test_rejects_matching_title_with_different_author():
    result = select_candidate(
        item("The God of the Woods", "Liz Moore"),
        [cand("The God of the Woods", "Someone Else")],
    )
    assert result.candidate is None
    assert result.reason == "no_confident_match"


def test_rejects_title_that_merely_contains_the_wanted_title():
    """'Principles' must not match 'Principles for Success'."""
    result = select_candidate(
        item("Principles", "Ray Dalio"),
        [cand("Principles for Success", "Ray Dalio")],
    )
    assert result.candidate is None
    assert result.reason == "no_confident_match"


def test_rejects_non_english_edition():
    result = select_candidate(
        item("1Q84 (1Q84, #1-3)", "Haruki Murakami"),
        [cand("1Q84", "Haruki Murakami", language="Russian")],
    )
    assert result.candidate is None
    assert result.reason == "no_english_edition"


def test_rejects_file_below_size_floor():
    """A 154-byte rate-limit stub was once imported as Pride and Prejudice."""
    result = select_candidate(
        item("Pride and Prejudice", "Jane Austen"),
        [cand("Pride and Prejudice", "Jane Austen", size_bytes=154)],
    )
    assert result.candidate is None
    assert result.reason == "no_confident_match"


def test_empty_candidate_list_is_not_found():
    result = select_candidate(item("May We Feed the King", "Rebecca Perry"), [])
    assert result.candidate is None
    assert result.reason == "not_found"


# --------------------------------------------------------------------------- #
# Matches that must succeed                                                    #
# --------------------------------------------------------------------------- #

def test_matches_despite_series_suffix():
    result = select_candidate(
        item("Strange Houses (Strange Houses, #1)", "Uketsu"),
        [cand("Strange Houses", "Uketsu")],
    )
    assert result.candidate is not None
    assert result.reason == "title_author"


def test_matches_despite_subtitle_on_the_candidate():
    result = select_candidate(
        item("The Dark Forest (Remembrance of Earth's Past, #2)", "Cixin Liu"),
        [cand("The Dark Forest: A Novel", "Cixin Liu")],
    )
    assert result.candidate is not None


def test_matches_ignoring_accents_and_punctuation():
    result = select_candidate(
        item("A Gentleman in Moscow", "Amor Towles"),
        [cand("A Gentleman in Moscow!", "Towles, Amor")],
    )
    assert result.candidate is not None


def test_isbn_match_wins_and_is_reported_as_such():
    result = select_candidate(
        item("Strange Houses (Strange Houses, #1)", "Uketsu", isbn="006343315X"),
        [cand("Whatever The Edition Is Called", "Uketsu", md5="b" * 32)],
        isbn_matched_md5s={"b" * 32},
    )
    assert result.candidate is not None
    assert result.reason == "isbn"


# --------------------------------------------------------------------------- #
# Choosing between valid candidates                                            #
# --------------------------------------------------------------------------- #

def test_prefers_epub_over_pdf():
    result = select_candidate(
        item("Neuromancer (Sprawl, #1)", "William Gibson"),
        [
            cand("Neuromancer", "William Gibson", ext="pdf", md5="c" * 32),
            cand("Neuromancer", "William Gibson", ext="epub", md5="d" * 32),
        ],
    )
    assert result.candidate.ext == "epub"


def test_format_preference_order_falls_back_through_ereader_formats():
    result = select_candidate(
        item("Neuromancer", "William Gibson"),
        [
            cand("Neuromancer", "William Gibson", ext="pdf", md5="c" * 32),
            cand("Neuromancer", "William Gibson", ext="fb2", md5="e" * 32),
            cand("Neuromancer", "William Gibson", ext="azw3", md5="f" * 32),
        ],
    )
    assert result.candidate.ext == "azw3"


def test_prefers_larger_file_within_the_same_format():
    result = select_candidate(
        item("Neuromancer", "William Gibson"),
        [
            cand("Neuromancer", "William Gibson", md5="c" * 32, size_bytes=400_000),
            cand("Neuromancer", "William Gibson", md5="d" * 32, size_bytes=900_000),
        ],
    )
    assert result.candidate.size_bytes == 900_000


def test_rejects_unsupported_extension():
    result = select_candidate(
        item("Fluviul Soaptelor", "Marian Coman"),
        [cand("Fluviul Soaptelor", "Marian Coman", ext="docx")],
    )
    assert result.candidate is None


@pytest.mark.parametrize("placeholder", [
    "Untitled (A Court of Thorns and Roses, #6)",
    "Untitled (The Empyrean, #4)",
])
def test_all_untitled_placeholders_are_skipped(placeholder):
    result = select_candidate(item(placeholder, "Sarah J. Maas"), [])
    assert result.reason == "placeholder_title"


# --------------------------------------------------------------------------- #
# Edition noise in libgen titles                                               #
#                                                                              #
# libgen appends ISBNs and edition wording into the title cell. Requiring exact #
# equality rejected real matches ('Neuromancer 20th anniversary 04410120'), so  #
# trailing noise is tolerated -- but only noise, never new content words.       #
# --------------------------------------------------------------------------- #

def test_matches_through_trailing_isbn_digits():
    result = select_candidate(
        item("Neuromancer (Sprawl, #1)", "William Gibson"),
        [cand("Neuromancer 9780441569595 0441569595", "William Gibson")],
    )
    assert result.candidate is not None


def test_matches_through_edition_wording():
    result = select_candidate(
        item("Neuromancer", "William Gibson"),
        [cand("Neuromancer 20th anniversary edition", "William Gibson")],
    )
    assert result.candidate is not None


def test_matches_novel_suffix():
    result = select_candidate(
        item("The God of the Woods", "Liz Moore"),
        [cand("The God of the Woods: A Novel", "Liz Moore")],
    )
    assert result.candidate is not None


def test_still_rejects_a_different_book_in_the_same_series():
    result = select_candidate(
        item("Dune", "Frank Herbert"),
        [cand("Dune Messiah", "Frank Herbert")],
    )
    assert result.candidate is None


def test_still_rejects_a_longer_different_title():
    result = select_candidate(
        item("The Power", "Naomi Alderman"),
        [cand("The Power of Habit", "Naomi Alderman")],
    )
    assert result.candidate is None


def test_matches_reversed_author_order():
    """Goodreads writes 'Liu Cixin'; libgen writes 'Cixin Liu'."""
    result = select_candidate(
        item("The Dark Forest (Remembrance of Earth's Past, #2)", "Liu Cixin"),
        [cand("The Dark Forest", "Cixin Liu")],
    )
    assert result.candidate is not None


# --------------------------------------------------------------------------- #
# Volume markers are not edition noise                                         #
#                                                                              #
# Caught by the go-live replay: 'In Search of Lost Time' fetched 'Volume 5: The #
# Captive'. A volume or book number changes which book it is, unlike wording    #
# such as 'anniversary edition', so it must never be treated as noise.          #
# --------------------------------------------------------------------------- #

def test_rejects_a_single_volume_of_a_longer_work():
    result = select_candidate(
        item("In Search of Lost Time", "Marcel Proust"),
        [cand("In Search of Lost Time, Volume 5: The Captive", "Marcel Proust")],
    )
    assert result.candidate is None


def test_rejects_a_volume_hidden_in_the_subtitle():
    """She shelved the 1Q84 #1-3 omnibus; 'Book 3' is a different book."""
    result = select_candidate(
        item("1Q84 (1Q84, #1-3)", "Haruki Murakami"),
        [cand("1Q84: Book 3", "Haruki Murakami")],
    )
    assert result.candidate is None


def test_still_matches_when_the_subtitle_is_not_a_volume_marker():
    result = select_candidate(
        item("Dungeon Crawler Carl (Dungeon Crawler Carl, #1)", "Matt Dinniman"),
        [cand("Dungeon Crawler Carl: Dungeon Crawler Carl", "Matt Dinniman")],
    )
    assert result.candidate is not None


def test_rejects_a_partial_omnibus():
    """'1Q84: Books 1 and 2' is two thirds of the #1-3 omnibus she shelved."""
    result = select_candidate(
        item("1Q84 (1Q84, #1-3)", "Haruki Murakami"),
        [cand("1Q84: Books 1 and 2", "Haruki Murakami")],
    )
    assert result.candidate is None
