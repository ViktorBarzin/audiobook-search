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


# --------------------------------------------------------------------------- #
# Size, because our outbound relay caps a message well below Amazon's limit    #
# --------------------------------------------------------------------------- #

from backend.kindle import MAX_BOOK_BYTES  # noqa: E402


def test_a_book_within_the_limit_is_chosen():
    chosen, skip = choose_kindle_format(["epub"], sizes={"epub": 1_000_000})
    assert chosen == "epub"
    assert skip is None


def test_an_oversized_book_is_skipped_with_its_size_in_the_reason():
    """Strange Houses was 23.2 MB and bounced off the relay at 31.7 MB encoded."""
    chosen, skip = choose_kindle_format(["epub"], sizes={"epub": 24_000_000})
    assert chosen is None
    assert skip and "22.9" in skip  # 24_000_000 bytes as MB, so the reason is concrete
    assert "relay" in skip.lower()


def test_a_smaller_suitable_format_is_used_when_the_preferred_one_is_too_big():
    chosen, skip = choose_kindle_format(
        ["epub", "azw3"], sizes={"epub": 30_000_000, "azw3": 2_000_000},
    )
    assert chosen == "azw3"
    assert skip is None


def test_sizes_are_optional_so_the_plain_call_still_works():
    chosen, skip = choose_kindle_format(["epub"])
    assert chosen == "epub"
    assert skip is None


def test_the_limit_leaves_headroom_for_base64_overhead():
    """A message is ~1.37x the file, and the relay caps the message at 20 MiB."""
    assert MAX_BOOK_BYTES * 1.37 < 20 * 1024 * 1024
