"""Tests for finding a freshly-imported book's Calibre id.

Observed live on 2026-08-16: a 24 MB epub imported correctly (it became book 497)
but the OPDS poll gave up first and returned -1, so the book was never added to
Anca's shelf. Since the shelf is the point of the pipeline, the id is now also
looked up directly in the library database, which knows the answer as soon as
Calibre has written it.
"""

import sqlite3

import pytest

from backend import main as bs_main


@pytest.fixture()
def library(tmp_path, monkeypatch):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, timestamp TEXT);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
    """)
    conn.executemany("INSERT INTO books VALUES (?,?,?)", [
        (495, "Some Other Book", "2026-08-01 00:00:00+00:00"),
        (497, "Strange Houses", "2026-08-16 12:02:36+00:00"),
    ])
    conn.execute("INSERT INTO authors VALUES (1,'Uketsu')")
    conn.execute("INSERT INTO books_authors_link VALUES (497,1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(tmp_path))
    return tmp_path


def test_finds_the_book_by_title(library):
    assert bs_main._calibre_id_for("Strange Houses", "Uketsu") == 497


def test_matches_when_goodreads_adds_a_series_suffix(library):
    assert bs_main._calibre_id_for("Strange Houses (Strange Houses, #1)", "Uketsu") == 497


def test_returns_none_for_a_book_that_is_not_there(library):
    assert bs_main._calibre_id_for("May We Feed the King", "Rebecca Perry") is None


def test_returns_none_when_the_library_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(tmp_path / "nope"))
    assert bs_main._calibre_id_for("Strange Houses", "Uketsu") is None


def test_prefers_the_newest_row_when_titles_repeat(library, tmp_path):
    conn = sqlite3.connect(tmp_path / "metadata.db")
    conn.execute("INSERT INTO books VALUES (?,?,?)",
                 (499, "Strange Houses", "2026-08-16 13:00:00+00:00"))
    conn.commit()
    conn.close()

    assert bs_main._calibre_id_for("Strange Houses", "Uketsu") == 499
