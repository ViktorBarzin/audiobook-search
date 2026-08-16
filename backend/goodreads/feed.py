"""Read a public Goodreads shelf over RSS.

Goodreads offers no WebSub hub, so there is nothing to subscribe to — but the
feed does answer `If-None-Match` with `304`, which makes a short poll interval
inexpensive: a quiet check costs a few hundred bytes rather than ~380 KB.

Sorting by `date_added` descending means page one already contains every new
addition, so a normal poll never needs to walk pagination.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import Enum

import httpx

from backend.goodreads.matcher import ShelfItem

logger = logging.getLogger(__name__)

FEED_URL = "https://www.goodreads.com/review/list_rss/{user_id}"

# Wikimedia and Goodreads both prefer a descriptive agent over a spoofed browser.
USER_AGENT = "book-search/1.0 (https://viktorbarzin.me; me@viktorbarzin.me)"

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
# Goodreads puts an <xhtml:meta> element between <channel> and its <title>, so the
# two are not adjacent; the channel title is simply the first one before any item.
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


class FeedStatus(Enum):
    OK = "ok"
    NOT_MODIFIED = "not_modified"


class FeedError(RuntimeError):
    """The feed could not be read, or did not describe the shelf we asked for."""


@dataclass
class FeedResult:
    status: FeedStatus
    items: list[ShelfItem] = field(default_factory=list)
    etag: str | None = None


def _tag(name: str, blob: str) -> str:
    match = re.search(
        rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", blob, re.S
    )
    return match.group(1).strip() if match else ""


def _parse_added(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        logger.warning("Unparseable user_date_added: %r", value)
        return None


def parse_items(xml: str) -> list[ShelfItem]:
    items = []
    for blob in _ITEM_RE.findall(xml):
        book_id = _tag("book_id", blob)
        title = _tag("title", blob)
        if not book_id or not title:
            continue
        items.append(ShelfItem(
            book_id=book_id,
            title=title,
            author=_tag("author_name", blob),
            isbn=_tag("isbn", blob) or None,
            added_at=_parse_added(_tag("user_date_added", blob)),
        ))
    return items


def assert_shelf(xml: str, shelf: str) -> None:
    """Confirm the feed really describes the shelf we requested.

    An unknown slug does not error: Goodreads returns 200 and serves the `read`
    shelf, whose channel title ends in a trailing space. Comparing on the exact
    suffix catches both that fallback and any future rename.
    """
    header = xml.split("<item>", 1)[0]
    match = _TITLE_RE.search(header)
    if not match:
        raise FeedError("feed has no channel title")
    title = match.group(1)
    if not title.rstrip().endswith(f"bookshelf: {shelf}"):
        raise FeedError(f"unexpected shelf in feed: {title!r} (wanted {shelf!r})")


async def fetch_shelf(
    client: httpx.AsyncClient,
    user_id: str,
    shelf: str,
    etag: str | None = None,
    per_page: int = 100,
) -> FeedResult:
    """Fetch one page of the shelf, newest first."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag

    try:
        response = await client.get(
            FEED_URL.format(user_id=user_id),
            params={
                "shelf": shelf,
                "per_page": str(per_page),
                "page": "1",
                "sort": "date_added",
                "order": "d",
            },
            headers=headers,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise FeedError(f"feed request failed: {exc}") from exc

    if response.status_code == 304:
        return FeedResult(status=FeedStatus.NOT_MODIFIED, etag=etag)

    if response.status_code != 200:
        raise FeedError(f"feed returned HTTP {response.status_code}")

    assert_shelf(response.text, shelf)
    return FeedResult(
        status=FeedStatus.OK,
        items=parse_items(response.text),
        etag=response.headers.get("ETag"),
    )
