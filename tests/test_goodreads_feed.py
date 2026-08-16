"""Feed-reader tests.

The wrong-shelf case is the important one: Goodreads answers an invalid shelf
slug with HTTP 200 serving the *read* shelf instead of an error, so without the
title assertion a typo would look like 526 brand-new additions to download.
"""

import httpx
import pytest

from backend.goodreads.feed import FeedError, FeedStatus, fetch_shelf

# Mirrors the real feed: Goodreads inserts an xhtml:meta element between
# <channel> and <title>, so the two are not adjacent.
FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
    <xhtml:meta xmlns:xhtml="http://www.w3.org/1999/xhtml" name="robots" content="noindex" />
<title>{channel_title}</title>
{items}
</channel>
</rss>"""

ITEM = """<item>
  <title>{title}</title>
  <book_id>{book_id}</book_id>
  <author_name>{author}</author_name>
  <isbn>{isbn}</isbn>
  <user_date_added>{added}</user_date_added>
</item>"""


def build_feed(items, channel_title="Anca E.'s bookshelf: to-read"):
    return FEED_TEMPLATE.format(
        channel_title=channel_title,
        items="\n".join(ITEM.format(**i) for i in items),
    )


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


ONE_ITEM = [{
    "title": "Strange Houses (Strange Houses, #1)",
    "book_id": "218671839",
    "author": "Uketsu",
    "isbn": "006343315X",
    "added": "Sat, 15 Aug 2026 10:19:09 -0700",
}]


async def test_parses_items():
    def handler(request):
        return httpx.Response(200, text=build_feed(ONE_ITEM), headers={"ETag": 'W/"abc"'})

    async with make_client(handler) as client:
        result = await fetch_shelf(client, "33074940", "to-read")

    assert result.status is FeedStatus.OK
    assert result.etag == 'W/"abc"'
    assert len(result.items) == 1
    item = result.items[0]
    assert item.book_id == "218671839"
    assert item.title == "Strange Houses (Strange Houses, #1)"
    assert item.author == "Uketsu"
    assert item.isbn == "006343315X"
    assert item.added_at.year == 2026


async def test_missing_isbn_becomes_none():
    items = [dict(ONE_ITEM[0], isbn="")]

    def handler(request):
        return httpx.Response(200, text=build_feed(items))

    async with make_client(handler) as client:
        result = await fetch_shelf(client, "33074940", "to-read")

    assert result.items[0].isbn is None


async def test_not_modified_short_circuits():
    seen = {}

    def handler(request):
        seen["if_none_match"] = request.headers.get("if-none-match")
        return httpx.Response(304)

    async with make_client(handler) as client:
        result = await fetch_shelf(client, "33074940", "to-read", etag='W/"abc"')

    assert result.status is FeedStatus.NOT_MODIFIED
    assert result.items == []
    assert seen["if_none_match"] == 'W/"abc"'


async def test_wrong_shelf_is_rejected():
    """An invalid slug serves the read shelf, titled with a trailing space."""
    def handler(request):
        return httpx.Response(200, text=build_feed(ONE_ITEM, "Anca E.'s bookshelf: read "))

    async with make_client(handler) as client:
        with pytest.raises(FeedError, match="unexpected shelf"):
            await fetch_shelf(client, "33074940", "to-read")


async def test_requests_newest_first():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text=build_feed(ONE_ITEM))

    async with make_client(handler) as client:
        await fetch_shelf(client, "33074940", "to-read")

    assert "sort=date_added" in seen["url"]
    assert "order=d" in seen["url"]
    assert "shelf=to-read" in seen["url"]


async def test_http_error_raises_feed_error():
    def handler(request):
        return httpx.Response(503)

    async with make_client(handler) as client:
        with pytest.raises(FeedError):
            await fetch_shelf(client, "33074940", "to-read")
