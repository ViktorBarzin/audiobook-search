"""ISBN normalization.

Goodreads publishes ISBN-10 for most shelf items. libgen's identifier index only
answers to ISBN-13 — a raw ISBN-10 query returns zero rows rather than an error,
so converting first is what makes ISBN matching work at all.
"""

from __future__ import annotations

import re

_NON_ISBN_RE = re.compile(r"[^0-9Xx]")


def _check_digit_13(core12: str) -> str:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core12))
    return str((10 - total % 10) % 10)


def to_isbn13(isbn: str | None) -> str | None:
    """Return the ISBN-13 form, or None if the input isn't a usable ISBN."""
    cleaned = _NON_ISBN_RE.sub("", isbn or "")

    if len(cleaned) == 13 and cleaned.isdigit():
        return cleaned

    if len(cleaned) == 10:
        core = "978" + cleaned[:9]
        if not core.isdigit():
            return None
        return core + _check_digit_13(core)

    return None
