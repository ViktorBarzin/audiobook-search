"""Which formats are worth putting on a Kindle."""

import pytest

from backend.kindle import KINDLE_FORMATS, choose_kindle_format


@pytest.mark.parametrize("fmt", KINDLE_FORMATS)
def test_a_reflowable_format_is_chosen(fmt):
    chosen, skip = choose_kindle_format([fmt])
    assert chosen == fmt
    assert skip is None


def test_epub_wins_when_several_formats_exist():
    """epub is what Amazon handles best, so it beats the older Kindle formats."""
    chosen, skip = choose_kindle_format(["mobi", "pdf", "epub", "azw3"])
    assert chosen == "epub"
    assert skip is None


def test_preference_order_is_honoured_without_epub():
    chosen, _ = choose_kindle_format(["mobi", "azw3"])
    assert chosen == "azw3"


def test_a_pdf_only_book_is_skipped_with_a_reason():
    """A PDF keeps its fixed layout on a 6in screen, so it is not worth sending."""
    chosen, skip = choose_kindle_format(["pdf"])
    assert chosen is None
    assert skip and "pdf" in skip.lower()


def test_no_formats_at_all_is_skipped_not_crashed():
    chosen, skip = choose_kindle_format([])
    assert chosen is None
    assert skip


def test_case_and_dots_do_not_defeat_the_match():
    """Calibre reports formats uppercase; OPDS paths want them lowercase."""
    chosen, skip = choose_kindle_format(["EPUB"])
    assert chosen == "epub"
    assert skip is None


def test_an_unknown_format_is_ignored_rather_than_sent():
    chosen, skip = choose_kindle_format(["cbz", "djvu"])
    assert chosen is None
    assert skip
