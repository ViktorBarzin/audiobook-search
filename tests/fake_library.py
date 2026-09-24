"""A throwaway Calibre metadata.db, laid out like the real one.

Only the tables book-search reads: books, authors, their link table, and data
for the formats on disk.
"""

import sqlite3


def make_library(directory, books):
    """Write metadata.db into `directory`.

    `books` is a list of (id, title, author, formats), where formats maps a
    format name such as "EPUB" to its size in bytes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "metadata.db")
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, timestamp TEXT);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
        CREATE TABLE data (book INTEGER, format TEXT, uncompressed_size INTEGER);
    """)
    for n, (book_id, title, author, formats) in enumerate(books, start=1):
        conn.execute("INSERT INTO books VALUES (?,?,?)",
                     (book_id, title, f"2026-09-24 06:{n:02d}:00+00:00"))
        conn.execute("INSERT INTO authors VALUES (?,?)", (book_id, author))
        conn.execute("INSERT INTO books_authors_link VALUES (?,?)", (book_id, book_id))
        for fmt, size in (formats or {}).items():
            conn.execute("INSERT INTO data VALUES (?,?,?)", (book_id, fmt, size))
    conn.commit()
    conn.close()
    return directory
