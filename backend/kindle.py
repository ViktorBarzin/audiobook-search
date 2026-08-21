"""Which of a book's formats is worth sending to a Kindle.

Amazon's Send-to-Kindle accepts more than this, PDF included, but accepting and
being readable are different things: a PDF keeps its fixed page layout on a 6in
screen, so it arrives as something you pinch and pan rather than read. The
automatic Goodreads path therefore only forwards reflowable formats and leaves
anything else in Calibre, where it is still one click away by hand.
"""

from __future__ import annotations

import os

# Our outbound relay (Brevo) refuses a message over 20 MiB, which is the binding
# limit here — Amazon's own Send-to-Kindle ceiling is much higher. A MIME message
# runs about 1.37x the attached file once base64 and headers are counted, so the
# file itself has to stay comfortably under 20 MiB / 1.37. Strange Houses, a
# 23.2 MB epub, produced a 31.7 MB message and bounced with dsn=5.3.4 on
# 2026-08-21, which is what this exists to prevent.
MAX_BOOK_BYTES = int(os.getenv("KINDLE_MAX_BOOK_BYTES", str(14 * 1024 * 1024)))

# Preference order, best first. epub is what Amazon converts most faithfully;
# azw3 and mobi are its own older formats and go through untouched.
KINDLE_FORMATS = ("epub", "azw3", "mobi")

# Named so the skip message can say something specific about the common case.
FIXED_LAYOUT_FORMATS = ("pdf",)


def _normalize(fmt: str) -> str:
    """Calibre records formats as 'EPUB'; OPDS download paths want 'epub'."""
    return (fmt or "").strip().lstrip(".").lower()


def choose_kindle_format(available, sizes=None) -> tuple[str | None, str | None]:
    """Pick the format to send, or say why nothing is going.

    Returns (format, skip_reason) with exactly one of the two set, so a caller
    can report a deliberate skip without it looking like a failure.

    `sizes` maps format to bytes and is optional: pass it and a format too large
    for the relay is passed over, which can mean falling through to a smaller
    format that is otherwise less preferred.
    """
    formats = {_normalize(f) for f in (available or [])}
    formats.discard("")
    sizes = {_normalize(k): v for k, v in (sizes or {}).items()}

    oversized: list[tuple[str, int]] = []
    for candidate in KINDLE_FORMATS:
        if candidate not in formats:
            continue
        size = sizes.get(candidate)
        if size is not None and size > MAX_BOOK_BYTES:
            oversized.append((candidate, size))
            continue
        return candidate, None

    if oversized:
        fmt, size = oversized[0]
        return None, (
            f"the {fmt} is {size / 1048576:.1f} MB, over the "
            f"{MAX_BOOK_BYTES / 1048576:.0f} MB our mail relay accepts"
        )

    if not formats:
        return None, "no downloadable format in Calibre"

    fixed = sorted(formats & set(FIXED_LAYOUT_FORMATS))
    if fixed:
        return None, f"only {'/'.join(fixed)} available, which does not reflow on a Kindle"

    return None, f"no Kindle-readable format (has {'/'.join(sorted(formats))})"
