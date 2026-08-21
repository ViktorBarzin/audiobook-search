"""Which of a book's formats is worth sending to a Kindle.

Amazon's Send-to-Kindle accepts more than this, PDF included, but accepting and
being readable are different things: a PDF keeps its fixed page layout on a 6in
screen, so it arrives as something you pinch and pan rather than read. The
automatic Goodreads path therefore only forwards reflowable formats and leaves
anything else in Calibre, where it is still one click away by hand.
"""

from __future__ import annotations

# Preference order, best first. epub is what Amazon converts most faithfully;
# azw3 and mobi are its own older formats and go through untouched.
KINDLE_FORMATS = ("epub", "azw3", "mobi")

# Named so the skip message can say something specific about the common case.
FIXED_LAYOUT_FORMATS = ("pdf",)


def _normalize(fmt: str) -> str:
    """Calibre records formats as 'EPUB'; OPDS download paths want 'epub'."""
    return (fmt or "").strip().lstrip(".").lower()


def choose_kindle_format(available) -> tuple[str | None, str | None]:
    """Pick the format to send, or say why nothing is going.

    Returns (format, skip_reason) with exactly one of the two set, so a caller
    can report a deliberate skip without it looking like a failure.
    """
    formats = {_normalize(f) for f in (available or [])}
    formats.discard("")

    for candidate in KINDLE_FORMATS:
        if candidate in formats:
            return candidate, None

    if not formats:
        return None, "no downloadable format in Calibre"

    fixed = sorted(formats & set(FIXED_LAYOUT_FORMATS))
    if fixed:
        return None, f"only {'/'.join(fixed)} available, which does not reflow on a Kindle"

    return None, f"no Kindle-readable format (has {'/'.join(sorted(formats))})"
