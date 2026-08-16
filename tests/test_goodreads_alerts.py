"""What the #alerts messages say.

Every book ends in a message either way — one when it reaches Anca's shelf, one
when it does not. The miss is the one that has to do work: Viktor searches
Anna's Archive by hand from it, so it carries a ready-made search link and the
ISBN rather than just a title.
"""

from backend.goodreads.matcher import ShelfItem
from backend.goodreads.sync import annas_search_url, format_miss, format_success


def item(title="May We Feed the King", author="Rebecca Perry", isbn="1803513888"):
    return ShelfItem(book_id="1", title=title, author=author, isbn=isbn, added_at=None)


def test_miss_names_the_book_and_the_reason():
    text = format_miss(item(), "not_found")
    assert "May We Feed the King" in text
    assert "Rebecca Perry" in text
    assert "not_found" in text


def test_miss_carries_a_ready_made_annas_archive_search():
    text = format_miss(item(), "not_found")
    assert "annas-archive" in text
    assert "May+We+Feed+the+King" in text or "May%20We%20Feed%20the%20King" in text


def test_miss_includes_the_isbn_when_there_is_one():
    assert "1803513888" in format_miss(item(), "not_found")


def test_miss_without_an_isbn_still_reads_cleanly():
    text = format_miss(item(isbn=None), "not_found")
    assert "ISBN" not in text
    assert "annas-archive" in text


def test_search_url_uses_title_and_author():
    url = annas_search_url(item())
    assert "annas-archive" in url
    assert "Perry" in url or "perry" in url


def test_search_url_is_safe_for_odd_titles():
    url = annas_search_url(item(title="Cântec deasupra cenușii & Co: #1", author="Ramona Gabăr"))
    assert " " not in url and "#" not in url.split("?", 1)[1].split("=", 1)[0]


def test_success_names_the_book_and_where_it_went():
    text = format_success(item(title="Middlemarch", author="George Eliot"), "epub", "isbn")
    assert "Middlemarch" in text
    assert "shelf" in text.lower()
    assert "epub" in text


def test_already_owned_book_is_reported_too():
    """Every book she adds ends in exactly one line — silence reads as nothing happened."""
    from backend.goodreads.sync import format_owned

    text = format_owned(item(title="The Bell Jar", author="Sylvia Plath"))
    assert "The Bell Jar" in text
    assert "already" in text.lower()
