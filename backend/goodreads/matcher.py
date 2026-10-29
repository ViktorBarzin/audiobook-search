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

# What libgen, Claude and people write for one language, folded. libgen names
# languages in English ("Bulgarian"); Claude reading a cover may answer with a
# code or the language's own name. A language missing here still matches when
# both sides spell it the same way.
LANGUAGE_ALIASES = {
    "english": ENGLISH,
    "bulgarian": {"bg", "bul", "български", "bulgarski"},
    "russian": {"ru", "rus", "русский"},
    "ukrainian": {"uk", "ukr", "українська"},
    "german": {"de", "deu", "ger", "deutsch"},
    "french": {"fr", "fra", "fre", "francais"},
    "spanish": {"es", "spa", "espanol", "castellano"},
    "italian": {"it", "ita", "italiano"},
    "portuguese": {"pt", "por", "portugues"},
    "romanian": {"ro", "ron", "rum", "romana"},
    "greek": {"el", "ell", "gre", "ελληνικα"},
    "polish": {"pl", "pol", "polski"},
    "dutch": {"nl", "nld", "dut", "nederlands"},
    "turkish": {"tr", "tur", "turkce"},
    "serbian": {"sr", "srp", "српски", "srpski"},
    "macedonian": {"mk", "mkd", "mac", "македонски"},
    "japanese": {"ja", "jpn"},
    "chinese": {"zh", "zho", "chi"},
}
_ALIAS_TO_LANGUAGE = {alias: name for name, aliases in LANGUAGE_ALIASES.items()
                      for alias in (*aliases, name)}

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
# Letters of any script survive: a Bulgarian title is all Cyrillic, and folding
# it to ASCII left nothing to compare.
_PUNCT_RE = re.compile(r"[^\w ]|_")
_WS_RE = re.compile(r"\s+")


@dataclass
class ShelfItem:
    """One row from her Goodreads shelf."""

    book_id: str
    title: str
    author: str
    isbn: str | None
    added_at: datetime | None
    # Read by the Claude check, which compares meaning rather than strings.
    description: str | None = None
    published: str | None = None


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
    publisher: str | None = None
    year: str | None = None


@dataclass
class MatchResult:
    candidate: Candidate | None
    reason: str


# Pre-reform Cyrillic letters and their modern spelling: Russian before 1918,
# Bulgarian before 1945. A photo of an old edition reads in the old spelling
# while libgen lists the book in the modern one.
_OLD_CYRILLIC = str.maketrans({"ѣ": "е", "і": "и", "ѳ": "ф", "ѵ": "и", "ѫ": "ъ", "ѭ": "ю"})
# Both orthographies ended every hard-final word in ъ; neither modern one
# ends a word in it, so dropping it changes no modern title.
_FINAL_HARD_SIGN_RE = re.compile(r"ъ\b")


def _fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    folded = (text or "").lower().translate(_OLD_CYRILLIC)
    folded = _FINAL_HARD_SIGN_RE.sub("", folded)
    folded = unicodedata.normalize("NFKD", folded)
    # Dropping the combining marks turns "é" into "e" as the ASCII fold did,
    # and leaves other scripts' letters in place.
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
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


def canonical_language(language: str | None) -> str:
    """One name per language ("bulgarian" for "bg"), or the folded text itself."""
    folded = _fold(language or "")
    return _ALIAS_TO_LANGUAGE.get(folded, folded)


def in_language(language: str | None, target: str = "english") -> bool:
    """Does a language field name the target? libgen sometimes lists several
    ("Bulgarian, English"), and any one of them counts."""
    wanted = canonical_language(target) or "english"
    return any(canonical_language(part) == wanted
               for part in re.split(r"[,;/]", language or "") if part.strip())


def is_english(language: str | None) -> bool:
    return in_language(language, "english")


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
    # An ISBN-10 whose check digit is X ('006341905x') is still an ISBN.
    if re.fullmatch(r"\d{9}x", token):
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


def _language_known(candidate: Candidate) -> bool:
    return bool(_fold(candidate.language or ""))


def _is_usable(candidate: Candidate, allow_unknown_language: bool = False,
               language: str = "english") -> bool:
    """Format, size and language all acceptable.

    A blank language is unknown rather than foreign: libgen leaves the field
    empty on many English files (The Midnight Train, 2026-09-08). The Goodreads
    ranking passes allow_unknown_language and then accepts such a file only on a
    full title and author match, never on an ISBN hit alone. Other callers (the
    share path's title-only pick) keep requiring the language by name.
    """
    if candidate.ext.lower() not in FORMAT_PREFERENCE:
        return False
    if allow_unknown_language and not _language_known(candidate):
        pass
    elif not in_language(candidate.language, language):
        return False
    if candidate.size_bytes is not None and candidate.size_bytes < MIN_SIZE_BYTES:
        return False
    return True


def _rank(candidate: Candidate, language: str = "english") -> tuple[int, int, int]:
    """The language named before unknown, then best format, then the largest file."""
    unknown = 0 if in_language(candidate.language, language) else 1
    fmt = FORMAT_PREFERENCE.index(candidate.ext.lower())
    return (unknown, fmt, -(candidate.size_bytes or 0))


def rank_candidates(
    item: ShelfItem,
    candidates: list[Candidate],
    isbn_matched_md5s: set[str] | None = None,
    language: str = "english",
) -> list[tuple[Candidate, str]]:
    """Every candidate confidently this book, best first, with why it qualified.

    ISBN hits lead because the identifier settles identity; they must name the
    language (English unless asked otherwise), since no title reasoning backs
    them up. Title-and-author matches
    follow. The poller walks this list when Claude refuses the first pick.
    """
    if is_placeholder(item.title):
        return []
    isbn_md5s = isbn_matched_md5s or set()
    usable = [c for c in candidates if _is_usable(c, allow_unknown_language=True, language=language)]

    def rank(c: Candidate) -> tuple[int, int, int]:
        return _rank(c, language)

    by_isbn = sorted((c for c in usable if c.md5 in isbn_md5s and in_language(c.language, language)),
                     key=rank)
    confident = sorted(
        (c for c in usable
         if c not in by_isbn
         and _titles_agree(item.title, c.title) and _authors_agree(item.author, c.author)),
        key=rank,
    )
    return [(c, "isbn") for c in by_isbn] + [(c, "title_author") for c in confident]


def select_candidate(
    item: ShelfItem,
    candidates: list[Candidate],
    isbn_matched_md5s: set[str] | None = None,
    language: str = "english",
) -> MatchResult:
    """Pick the one file worth downloading for this shelf item, or explain why not.

    `isbn_matched_md5s` holds md5s the source returned for the item's ISBN. Those
    skip title reasoning entirely, since the identifier already settles identity.
    """
    if is_placeholder(item.title):
        return MatchResult(None, "placeholder_title")

    if not candidates:
        return MatchResult(None, "not_found")

    ranked = rank_candidates(item, candidates, isbn_matched_md5s, language=language)
    if ranked:
        candidate, reason = ranked[0]
        return MatchResult(candidate, reason)

    # Distinguish 'exists but not in this language' from 'nothing usable at
    # all', because only the first is worth reporting as a near-miss. Only a
    # language libgen actually names counts as foreign.
    if not any(_is_usable(c, allow_unknown_language=True, language=language) for c in candidates) and any(
        _language_known(c) and not in_language(c.language, language) for c in candidates
    ):
        english = canonical_language(language) in ("", "english")
        return MatchResult(None, "no_english_edition" if english else "no_edition_in_language")
    return MatchResult(None, "no_confident_match")
