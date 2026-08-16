"""Durable record of which shelf items have already been handled.

The pipeline makes one attempt per book, so this table is what stops a book being
searched forever — and what records *why* something never arrived, since there is
no review queue to look at. Outcomes are stored rather than inferred so a later
backfill can select exactly the rows worth revisiting.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    SEEDED = "seeded"            # present at first run; deliberately not fetched
    PENDING = "pending"          # a transient failure; still owed an attempt
    DOWNLOADED = "downloaded"
    OWNED = "owned"              # already in the Calibre library
    NOT_FOUND = "not_found"      # no source had it
    NO_MATCH = "no_match"        # candidates existed, none confidently hers
    SKIPPED = "skipped"          # placeholder for an unpublished book
    ERROR = "error"


SCHEMA = """
CREATE TABLE IF NOT EXISTS goodreads_seen (
    book_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    author       TEXT,
    isbn         TEXT,
    added_at     TIMESTAMPTZ,
    outcome      TEXT NOT NULL,
    reason       TEXT,
    md5          TEXT,
    calibre_id   INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE goodreads_seen ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS goodreads_seen_outcome_idx ON goodreads_seen (outcome);
"""


class MemorySeenStore:
    """In-memory store, used by tests and by dry runs."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def ensure_schema(self) -> None:
        pass

    def is_empty(self) -> bool:
        return not self._rows

    def known_ids(self) -> set[str]:
        """Books needing no further work. A pending row is deliberately absent."""
        return {k for k, v in self._rows.items() if v["outcome"] != Outcome.PENDING.value}

    def attempts_for(self, book_id: str) -> int:
        row = self._rows.get(book_id)
        return row.get("attempts", 0) if row else 0

    def defer(self, item, reason: str) -> int:
        """Record a transient failure and return how many attempts have now been made."""
        row = self._rows.setdefault(item.book_id, {"attempts": 0})
        row.update({"outcome": Outcome.PENDING.value, "title": item.title,
                    "author": item.author, "isbn": item.isbn, "reason": reason})
        row["attempts"] = row.get("attempts", 0) + 1
        return row["attempts"]

    def outcome(self, book_id: str) -> Outcome | None:
        row = self._rows.get(book_id)
        return Outcome(row["outcome"]) if row else None

    def mark_seeded(self, items) -> int:
        """Accepts ShelfItems or bare book_ids, matching the Postgres store."""
        for entry in items:
            book_id = getattr(entry, "book_id", entry)
            title = getattr(entry, "title", "")
            self._rows.setdefault(
                book_id, {"outcome": Outcome.SEEDED.value, "title": title},
            )
        return len(self._rows)

    def record(self, item, outcome: Outcome, reason=None, md5=None, calibre_id=None) -> None:
        attempts = self._rows.get(item.book_id, {}).get("attempts", 0)
        self._rows[item.book_id] = {
            "outcome": outcome.value, "title": item.title, "author": item.author,
            "isbn": item.isbn, "reason": reason, "md5": md5, "calibre_id": calibre_id,
            "attempts": attempts,
        }


class PostgresSeenStore:
    """Postgres-backed store on the shared dbaas cluster."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None

    def _connection(self):
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn

    def ensure_schema(self) -> None:
        with self._connection().cursor() as cur:
            cur.execute(SCHEMA)

    def is_empty(self) -> bool:
        with self._connection().cursor() as cur:
            cur.execute("SELECT 1 FROM goodreads_seen LIMIT 1")
            return cur.fetchone() is None

    def known_ids(self) -> set[str]:
        """Books needing no further work. A pending row is deliberately absent."""
        with self._connection().cursor() as cur:
            cur.execute("SELECT book_id FROM goodreads_seen WHERE outcome <> %s",
                        (Outcome.PENDING.value,))
            return {row[0] for row in cur.fetchall()}

    def attempts_for(self, book_id: str) -> int:
        with self._connection().cursor() as cur:
            cur.execute("SELECT attempts FROM goodreads_seen WHERE book_id = %s", (book_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def defer(self, item, reason: str) -> int:
        """Record a transient failure and return how many attempts have now been made."""
        with self._connection().cursor() as cur:
            cur.execute(
                """INSERT INTO goodreads_seen
                       (book_id, title, author, isbn, added_at, outcome, reason, attempts)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 1)
                   ON CONFLICT (book_id) DO UPDATE SET
                       outcome = EXCLUDED.outcome,
                       reason = EXCLUDED.reason,
                       attempts = goodreads_seen.attempts + 1,
                       processed_at = now()
                   RETURNING attempts""",
                (item.book_id, item.title, item.author, item.isbn, item.added_at,
                 Outcome.PENDING.value, reason),
            )
            return cur.fetchone()[0]

    def outcome(self, book_id: str) -> Outcome | None:
        with self._connection().cursor() as cur:
            cur.execute("SELECT outcome FROM goodreads_seen WHERE book_id = %s", (book_id,))
            row = cur.fetchone()
            return Outcome(row[0]) if row else None

    def mark_seeded(self, items) -> int:
        rows = [
            (i.book_id, i.title, i.author, i.isbn, i.added_at, Outcome.SEEDED.value)
            if hasattr(i, "book_id") else (i, "", None, None, None, Outcome.SEEDED.value)
            for i in items
        ]
        if not rows:
            return 0
        with self._connection().cursor() as cur:
            cur.executemany(
                """INSERT INTO goodreads_seen (book_id, title, author, isbn, added_at, outcome)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (book_id) DO NOTHING""",
                rows,
            )
        return len(rows)

    def record(self, item, outcome: Outcome, reason=None, md5=None, calibre_id=None) -> None:
        with self._connection().cursor() as cur:
            cur.execute(
                """INSERT INTO goodreads_seen
                       (book_id, title, author, isbn, added_at, outcome, reason, md5, calibre_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (book_id) DO UPDATE SET
                       outcome = EXCLUDED.outcome,
                       reason = EXCLUDED.reason,
                       md5 = EXCLUDED.md5,
                       calibre_id = EXCLUDED.calibre_id,
                       processed_at = now()""",
                (item.book_id, item.title, item.author, item.isbn, item.added_at,
                 outcome.value, reason, md5, calibre_id),
            )
