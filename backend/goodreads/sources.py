"""Turn source HTML into Candidates the matcher can reason about.

libgen.li serves a nine-column table whose Language column (index 4) the existing
scraper did not read. Language is needed here because an edition in a language
she doesn't read is a failed delivery even when the book is right.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from backend.goodreads.matcher import Candidate

logger = logging.getLogger(__name__)


class SourceUnavailable(RuntimeError):
    """The source could not be reached — distinct from 'the book isn't there'.

    Each book gets one attempt at being found, so a timeout must be told apart
    from a genuine absence: the first is retried next cycle, the second is final.
    """

# libgen renders sizes as '661 kB' / '4.2 MB', using decimal multiples.
_SIZE_RE = re.compile(r"([\d.]+)\s*([kmg]?b)\b", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000}

_MD5_RE = re.compile(r"md5=([a-f0-9]{32})", re.IGNORECASE)

# Column layout of the libgen.li file table.
_COL_TITLE, _COL_AUTHOR, _COL_LANG, _COL_SIZE, _COL_EXT = 0, 1, 4, 6, 7
_MIN_COLS = 9


def parse_size_bytes(text: str | None) -> int | None:
    match = _SIZE_RE.search(text or "")
    if not match:
        return None
    value, unit = match.groups()
    try:
        return int(float(value) * _SIZE_UNITS[unit.lower()])
    except (ValueError, KeyError):
        return None


def rows_to_candidates(html: str, source: str = "libgen") -> list[Candidate]:
    """Parse a libgen results table into Candidates.

    Cell text is extracted with a separator so nested markup does not fuse into
    strings like 'Neuromancer20th anniversary04410120', which is what made the
    earlier title comparisons unreliable.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-striped") or soup.find("table")
    if not table:
        return []

    candidates: list[Candidate] = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < _MIN_COLS:
            continue

        md5_match = _MD5_RE.search(str(row))
        if not md5_match:
            continue

        def cell(index: int) -> str:
            return cols[index].get_text(" ", strip=True)

        title = cell(_COL_TITLE)
        if not title:
            continue

        candidates.append(Candidate(
            md5=md5_match.group(1).lower(),
            title=title,
            author=cell(_COL_AUTHOR) or None,
            ext=cell(_COL_EXT).lower(),
            language=cell(_COL_LANG) or None,
            size_bytes=parse_size_bytes(cell(_COL_SIZE)),
            source=source,
        ))

    return candidates
