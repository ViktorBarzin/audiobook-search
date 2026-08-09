"""Duplicate detection for audiobook downloads.

Two independent failure modes produced duplicates in Audiobookshelf:

1. The only question asked was "does Audiobookshelf already have this?", which
   sees imported books but not ones still downloading. Two copies queued
   together therefore both passed. find_inflight_duplicate() closes that by
   checking qBittorrent as well.

2. Matching accepted "either title contains the other", which would treat
   "Principles" and "Principles for Success" as the same book and refuse a
   download the user actually wanted. A false positive is worse than a
   duplicate — a duplicate is visible and removable, a wrongly-refused download
   just looks like the feature is broken.

So is_same_book() is deliberately conservative: the author must match, and
either the full normalised titles are equal, or exactly one side carries a
subtitle and the main titles match. Requiring full equality when BOTH sides have
subtitles is what keeps series entries apart.
"""

import re

# Edition/format markers that describe the same book rather than a different one.
_EDITION_MARKERS = (
    "unabridged", "abridged", "dramatized adaptation", "dramatised adaptation",
    "audiobook", "audio book", "retail", "remastered",
)
_BRACKETED = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_SUBTITLE_SPLIT = re.compile(r"\s[-–—]\s|:")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# "Unknown Author" is book-search's own placeholder — it must not veto a match.
_WILDCARD_AUTHORS = {"", "unknown author", "unknown", "various", "various authors"}


def _strip_editions(text: str) -> str:
    """Drop edition/format markers, whether bracketed or bare."""
    def _drop_bracket(m: re.Match) -> str:
        inner = m.group(1).strip().lower()
        return "" if any(k in inner for k in _EDITION_MARKERS) else m.group(0)

    out = _BRACKETED.sub(_drop_bracket, text)
    for marker in _EDITION_MARKERS:
        out = re.sub(rf"\b{re.escape(marker)}\b", " ", out, flags=re.I)
    return out


def normalize_title(title: str | None) -> str:
    """Casefold, drop edition markers, and reduce punctuation to single spaces."""
    if not title:
        return ""
    return " ".join(_NON_ALNUM.sub(" ", _strip_editions(title).lower()).split())


def normalize_author(author: str | None) -> str:
    if not author:
        return ""
    return " ".join(_NON_ALNUM.sub(" ", author.lower()).split())


def _main_title(title: str) -> tuple[str, bool]:
    """Return (normalised main title, whether a subtitle was present)."""
    stripped = _strip_editions(title or "")
    parts = _SUBTITLE_SPLIT.split(stripped, maxsplit=1)
    main = normalize_title(parts[0])
    has_subtitle = len(parts) > 1 and bool(normalize_title(parts[1]))
    return main, has_subtitle


def authors_match(a: str | None, b: str | None) -> bool:
    """True when the authors agree, or either side is a placeholder."""
    na, nb = normalize_author(a), normalize_author(b)
    if na in _WILDCARD_AUTHORS or nb in _WILDCARD_AUTHORS:
        return True
    if na == nb:
        return True
    # Handles "Ray Dalio" vs "Ray Dalio, Jeremy Bobb" (narrator appended).
    return na in nb or nb in na


def is_same_book(title_a: str | None, author_a: str | None,
                 title_b: str | None, author_b: str | None) -> bool:
    """Conservative duplicate test — see the module docstring for the rules."""
    na, nb = normalize_title(title_a), normalize_title(title_b)
    if not na or not nb:
        return False
    if not authors_match(author_a, author_b):
        return False
    if na == nb:
        return True

    main_a, sub_a = _main_title(title_a)
    main_b, sub_b = _main_title(title_b)
    # Exactly one side carries a subtitle: "Principles" vs "Principles: Life and
    # Work" is the same book. If BOTH carry subtitles, differing subtitles mean
    # different books ("Dune: Book One" vs "Dune: Book Two"), so full equality
    # was already required above.
    if sub_a != sub_b and main_a and main_a == main_b:
        return True
    return False


def parse_save_path(save_path: str | None) -> tuple[str, str] | None:
    """Split book-search's own '/audiobooks/{author}/{title}' into its parts."""
    if not save_path:
        return None
    parts = [p for p in str(save_path).strip("/").split("/") if p]
    if len(parts) < 3 or parts[0] != "audiobooks":
        return None
    return parts[1], parts[2]


def find_inflight_duplicate(title: str, author: str, torrents: list[dict]):
    """Return the torrent already downloading this book, or None.

    Matches on save_path rather than the torrent's display name: save_path is
    book-search's own canonical layout, while names are uploader free-text.
    """
    for torrent in torrents or []:
        parsed = parse_save_path(torrent.get("save_path"))
        if not parsed:
            continue
        existing_author, existing_title = parsed
        if is_same_book(title, author, existing_title, existing_author):
            return torrent
    return None
