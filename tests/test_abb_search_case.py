"""Tests for AudioBookBay SEARCH — the capitalised-query path.

Measured live on 2026-08-21 against audiobookbay.lu. A query carrying any
uppercase letter is answered with a redirect to the site root, which serves the
nine newest uploads:

    /?s=Dalio&tt=1                -> final URL == BASE_URL, 9 front-page posts
    /?s=Ray+dalio&tt=1            -> final URL == BASE_URL, 9 front-page posts
    /?s=Ray+Dalio+Principles&tt=1 -> final URL == BASE_URL, 9 front-page posts
    /?s=dalio&tt=1                -> 6 posts, all genuinely Dalio
    /?s=ray+dalio&tt=1            -> 6 posts, all genuinely Dalio
    /?s=ray+dalio+principles&tt=1 -> 5 posts, all genuinely Dalio

Case is the only variable. Word count and the `tt=1` audiobooks-only filter were
both ruled out by the same probe, so `tt=1` stays.

`search()` followed that redirect and parsed the front page, so a search for
"Ray Dalio" answered with Bret Easton Ellis, West Coast Mobsters and Failure
Frame — nine well-formed rows with no relation to the query. Since every query a
human types is capitalised, that was the normal case rather than an edge one.

Two independent guards are pinned below, because either alone leaves a hole:
lowercasing stops us provoking the redirect, and treating "final URL is the site
root" as no-results stops us presenting the front page as matches whenever the
site bounces us for some other reason. That second guard is the same lesson as
the libgen silent-empty bug — never let a fall-through look like an answer.
"""

import httpx
import pytest

from backend.scraper import AudioBookBayScraper

BASE = AudioBookBayScraper.BASE_URL


def _post(title: str, path: str) -> str:
    return (
        f'<div class="post">'
        f'<h2 class="postTitle"><a href="/{path}/">{title}</a></h2>'
        f'<div class="postContent">'
        f'<p>Written by Ray Dalio Read by Ray Dalio, Jeremy Bobb '
        f'Format: M4B Size: 460 MBs</p>'
        f"</div>"
        f"</div>"
    )


# Titles taken from the live front page on 2026-08-21 — what a bounced search
# used to return.
FRONT_PAGE = "<html><body>" + "".join(
    _post(t, f"abss/{i}")
    for i, t in enumerate(
        [
            "Bret Easton Ellis Bibliography (1985-2023) - Bret Easton Ellis",
            "West Coast Mobsters: Castellani Family Series, Books 1-3",
            "With This Ring (Opposites Attract, Book 1) - RS McKenzie",
            "Read Herring Hunt: Mystery Bookshop, Book 2 - V.M. Burns",
            "Impact Winter: Seasons 1, 2 & 3 - Binaural Surround",
            "Failure Frame, Vol. 5 - Kaoru Shinozaki",
            "This Might Be Too Personal - Alyssa Shelasky",
            "Sex Diaries - Alyssa Shelasky",
            "Failure Frame, Vol. 1 - 4 - Kaoru Shinozaki",
        ]
    )
) + "</body></html>"

# What the live site returns for a lowercase "ray dalio principles".
REAL_HITS = (
    "<html><body>"
    + _post("Principles: Life and Work - Ray Dalio", "abss/principles-life-and-work")
    + _post("Principles for Navigating Big Debt Crises - Ray Dalio", "abss/big-debt-crises")
    + "</body></html>"
)


def _scraper(handler):
    s = AudioBookBayScraper()
    s.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    return s


def _bounce_on_uppercase(seen: list[str]):
    """Reproduce the live behaviour: any capital in `s` redirects to the root."""

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("s", "")
        if request.url.path == "/" and q:
            seen.append(q)
            if any(ch.isupper() for ch in q):
                return httpx.Response(302, headers={"Location": BASE + "/"})
            return httpx.Response(200, text=REAL_HITS)
        # The site root, i.e. where a bounced search lands.
        return httpx.Response(200, text=FRONT_PAGE)

    return handler


@pytest.mark.parametrize(
    "query",
    ["Ray Dalio", "Dalio", "Ray dalio", "Ray Dalio Principles", "RAY DALIO"],
)
async def test_capitalised_query_still_finds_the_book(query):
    """A capitalised query must return real matches, not the front page."""
    seen: list[str] = []
    results = await _scraper(_bounce_on_uppercase(seen)).search(query)

    assert [r.title for r in results] == [
        "Principles: Life and Work - Ray Dalio",
        "Principles for Navigating Big Debt Crises - Ray Dalio",
    ]
    # The request we actually sent carried no capitals, so the site never bounced.
    assert seen and not any(ch.isupper() for ch in seen[0])


async def test_lowercase_query_is_unchanged():
    """The path that already worked keeps working."""
    seen: list[str] = []
    results = await _scraper(_bounce_on_uppercase(seen)).search("ray dalio principles")

    assert len(results) == 2
    assert seen == ["ray dalio principles"]


async def test_bounce_to_front_page_is_no_results():
    """If the site bounces us to its root anyway, that is not a set of matches.

    Guards the case where the redirect is triggered by something other than
    capitalisation. Without this the nine front-page posts parse cleanly and are
    indistinguishable, from the caller's seat, from genuine hits.
    """

    def always_bounce(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("s"):
            return httpx.Response(302, headers={"Location": BASE + "/"})
        return httpx.Response(200, text=FRONT_PAGE)

    assert await _scraper(always_bounce).search("dune") == []


async def test_real_results_are_not_mistaken_for_a_bounce():
    """A genuine search page keeps its results even though it shares the path.

    The live search URL is `/?s=...`, i.e. the same path as the root — so the
    bounce check has to compare the full URL including the query, not the path.
    """

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=REAL_HITS)

    results = await _scraper(ok).search("dune")
    assert len(results) == 2


async def test_tt_filter_is_preserved():
    """`tt=1` narrows to audiobooks and is not what caused the bounce."""
    seen: dict[str, str] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, text=REAL_HITS)

    await _scraper(ok).search("Ray Dalio")
    assert seen.get("tt") == "1"
