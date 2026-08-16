"""Decide whether a search result is really the book Anca shelved.

The pipeline is autonomous — nothing reviews a pick before it reaches her shelf —
so these rules are deliberately strict. A miss costs one book she can be told
about; a false positive puts the wrong book in a shared library.

Two keys are used, in order of trust:

1. **ISBN.** Goodreads supplies one for ~77% of her additions and libgen indexes
   it, so an ISBN hit needs no title reasoning at all.
2. **Title + author.** Both must agree after normalization. Substring logic is
   avoided on purpose: 'Principles' contains-matching 'Principles for Success'
   is the failure mode that makes a library untrustworthy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

# Formats we accept, best first. Kindle and Calibre both handle epub properly;
# pdf is kept as a last resort because a page-image file still beats nothing,
# though it reads poorly on an e-reader.
FORMAT_PREFERENCE = ("epub", "azw3", "mobi", "fb2", "pdf")

# Matches the existing MIN_EBOOK_SIZE_BYTES floor in main.py. Anna's Archive has
# served 154-byte rate-limit stubs with HTTP 200; one was imported as a real book.
MIN_SIZE_BYTES = 5_000

ENGLISH = {"english", "en", "eng"}

# Words libgen appends to a title that describe the edition rather than the work.
# Anything outside this set in a trailing position means a different book.
EDITION_NOISE = {
    "a", "an", "the", "and",
    "novel", "edition", "editions", "ed", "classic", "classics", "anniversary",
    "illustrated", "unabridged", "abridged", "complete", "annotated", "deluxe",
    "reprint", "paperback", "hardcover", "hardback", "club",
    "series", "translated", "translation", "new",
    "revised", "updated", "international", "bestseller", "bestselling",
    "edicion", "edicao", "printing", "press", "publishing", "publisher",
}

# Goodreads carries rows for books that are announced but unwritten. They can
# never be found, so they are skipped rather than searched.
PLACEHOLDER_RE = re.compile(r"^\s*untitled\s*\(", re.IGNORECASE)

# Parser noise: a cell that still contains markup is not a book title.
HTML_NOISE_RE = re.compile(r"href=|<[a-z]+[ >]|edition\.php|\.php\?", re.IGNORECASE)

_SERIES_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
# 'Book 3', 'Volume 5', 'Part 2' — identifies which book, not which printing.
_VOLUME_RE = re.compile(r"^(books?|vols?|volumes?|parts?|no)\s*\d+")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]")
_WS_RE = re.compile(r"\s+")


@dataclass
class ShelfItem:
    """One row from her Goodreads shelf."""

    book_id: str
    title: str
    author: str
    isbn: str | None
    added_at: datetime | None


@dataclass
class Candidate:
    """One downloadable file offered by a source."""

    md5: str
    title: str
    author: str | None
    ext: str
    language: str | None
    size_bytes: int | None
    source: str


@dataclass
class MatchResult:
    candidate: Candidate | None
    reason: str


def _fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = folded.encode("ascii", "ignore").decode()
    folded = _PUNCT_RE.sub(" ", folded.lower())
    return _WS_RE.sub(" ", folded).strip()


def normalize_title(title: str) -> str:
    """Fold a title and drop the parts editions disagree about.

    Goodreads writes series membership as a trailing parenthetical
    ('Strange Houses (Strange Houses, #1)') while libgen usually does not, and
    either side may carry a subtitle after a colon.

    A subtitle that is really a volume marker ('1Q84: Book 3') is kept, because
    it identifies a different book rather than a different printing.
    """
    stripped = _SERIES_SUFFIX_RE.sub("", title or "")
    main, _, subtitle = stripped.partition(":")
    folded = _fold(main)
    if subtitle and _VOLUME_RE.match(_fold(subtitle)):
        folded = f"{folded} {_fold(subtitle)}".strip()
    return folded


def normalize_author(author: str | None) -> str:
    """Fold an author name and drop the ordering libgen and Goodreads disagree on."""
    folded = _fold(author or "")
    if "," in (author or ""):
        # 'Towles, Amor' and 'Amor Towles' are the same person.
        parts = [_fold(p) for p in author.split(",")]
        folded = " ".join(reversed(parts)).strip()
    return _WS_RE.sub(" ", folded).strip()


def author_surname(author: str | None) -> str:
    tokens = normalize_author(author).split()
    return tokens[-1] if tokens else ""


def is_placeholder(title: str) -> bool:
    return bool(PLACEHOLDER_RE.match(title or ""))


def looks_like_markup(text: str | None) -> bool:
    return bool(HTML_NOISE_RE.search(text or ""))


def is_english(language: str | None) -> bool:
    return _fold(language or "") in ENGLISH


def _authors_agree(wanted: str, offered: str | None) -> bool:
    """Require the surname to match and appear in the offered name.

    Full-string equality is too strict — libgen carries 'William Gibson [Gibson,
    William]' and initials-only forms — while a bare token-overlap check lets
    unrelated books through whenever a common surname appears.
    """
    surname = author_surname(wanted)
    if not surname or len(surname) < 2:
        return False
    offered_norm = normalize_author(offered)
    if not offered_norm:
        return False
    return surname in offered_norm.split()


def _is_noise_token(token: str) -> bool:
    """Is this trailing word bookkeeping rather than part of the title?

    libgen's title cell absorbs ISBNs, years, edition wording and stray letters
    from the surrounding markup ('Middlemarch: Classic b l 7').
    """
    if token.isdigit():
        return True
    if re.fullmatch(r"\d+(st|nd|rd|th)", token):
        return True
    if len(token) == 1:
        return True
    return token in EDITION_NOISE


def _titles_agree(wanted: str, offered: str | None) -> bool:
    """Compare titles, tolerating edition bookkeeping but not different books.

    Equality after normalization is the common case. Beyond that, the offered
    title may only *extend* the wanted one with noise tokens — a single real word
    in the remainder ('Dune' vs 'Dune Messiah', 'Principles' vs 'Principles for
    Success') means it is a different book.
    """
    if looks_like_markup(offered):
        return False
    left, right = normalize_title(wanted), normalize_title(offered or "")
    if not left or not right:
        return False
    if left == right:
        return True

    left_tokens, right_tokens = left.split(), right.split()
    if len(right_tokens) <= len(left_tokens):
        return False
    if right_tokens[: len(left_tokens)] != left_tokens:
        return False
    return all(_is_noise_token(t) for t in right_tokens[len(left_tokens):])


def _is_usable(candidate: Candidate) -> bool:
    if candidate.ext.lower() not in FORMAT_PREFERENCE:
        return False
    if not is_english(candidate.language):
        return False
    if candidate.size_bytes is not None and candidate.size_bytes < MIN_SIZE_BYTES:
        return False
    return True


def _rank(candidate: Candidate) -> tuple[int, int]:
    """Best format first, then the largest file within that format."""
    fmt = FORMAT_PREFERENCE.index(candidate.ext.lower())
    return (fmt, -(candidate.size_bytes or 0))


def select_candidate(
    item: ShelfItem,
    candidates: list[Candidate],
    isbn_matched_md5s: set[str] | None = None,
) -> MatchResult:
    """Pick the one file worth downloading for this shelf item, or explain why not.

    `isbn_matched_md5s` holds md5s the source returned for the item's ISBN. Those
    skip title reasoning entirely, since the identifier already settles identity.
    """
    if is_placeholder(item.title):
        return MatchResult(None, "placeholder_title")

    if not candidates:
        return MatchResult(None, "not_found")

    usable = [c for c in candidates if _is_usable(c)]
    if not usable:
        # Distinguish 'exists but not in English' from 'nothing usable at all',
        # because only the first is worth reporting as a near-miss.
        if any(c for c in candidates if not is_english(c.language)):
            return MatchResult(None, "no_english_edition")
        return MatchResult(None, "no_confident_match")

    by_isbn = [c for c in usable if c.md5 in (isbn_matched_md5s or set())]
    if by_isbn:
        return MatchResult(sorted(by_isbn, key=_rank)[0], "isbn")

    confident = [
        c for c in usable
        if _titles_agree(item.title, c.title) and _authors_agree(item.author, c.author)
    ]
    if confident:
        return MatchResult(sorted(confident, key=_rank)[0], "title_author")

    return MatchResult(None, "no_confident_match")
