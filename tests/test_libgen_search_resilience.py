"""Tests for libgen SEARCH resilience — the transient-failure path.

Measured live on 2026-08-16: `LibGenScraper.search("dune")` returned 25 results
seven times out of eight, and an empty list the eighth. libgen.li itself was
healthy throughout — root page 0.4-0.9s, search endpoint 0.74-2.53s, well inside
the 15s client timeout — so this is a connection-level blip, not a slow or
blocked mirror.

Two things turned that ~12% blip into a silent, total failure:

  * `_search_li` catches every exception and returns [], and `search` returns
    that straight to the caller. One dropped connection = zero results, with no
    retry and no attempt at the next mirror (libgen.vg answers in 2-4s).
  * the log line is `f"LibGen search failed: {e}"`, and these httpx
    connection errors stringify to "", so the operator sees
    `LibGen search failed:` and cannot tell what broke.

That is easy to misread from the outside as "my IP is blocked" — it looks
identical from the user's seat, and it sent us looking at VPN egress first.
These tests pin the retry, the mirror fall-through, and the diagnosable log.
"""

import logging

import httpx
import pytest

from backend.libgen import LibGenScraper

# Mirrors the live libgen.li table shape verified 2026-08-16: class
# "table table-striped", 9 columns, md5 carried in the last one.
ROW = (
    "<tr>"
    "<td>Dune</td><td>Frank Herbert</td><td>Ace</td><td>1965</td>"
    "<td>English</td><td>412</td><td>1 MB</td><td>epub</td>"
    '<td><a href="ads.php?md5=b8eef1eb09cab009626eb5eebb0223f4">GET</a></td>'
    "</tr>"
)
HEADER = "<tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th><th>Lang</th><th>Pages</th><th>Size</th><th>Ext</th><th>Mirrors</th></tr>"
RESULTS_HTML = f'<html><body><table class="table table-striped">{HEADER}{ROW}</table></body></html>'
EMPTY_HTML = f'<html><body><table class="table table-striped">{HEADER}</table></body></html>'


SEARCH_PATH = "/index.php"


def _scraper(search_handler, mirror="https://libgen.li"):
    """Build a scraper whose HEALTH CHECKS always pass.

    `_get_mirror()` issues its own GETs against each mirror root. If those share
    the handler under test they silently absorb the injected failures and every
    assertion below passes for the wrong reason — which is exactly what happened
    on the first run of this file. Only requests to SEARCH_PATH reach the
    handler; everything else is a healthy 200.
    """
    def route(request):
        if request.url.path != SEARCH_PATH:
            return httpx.Response(200, text="<html>ok</html>")
        return search_handler(request)

    s = LibGenScraper()
    s.client = httpx.AsyncClient(transport=httpx.MockTransport(route),
                                 follow_redirects=True)
    s._working_mirror = mirror
    return s


class _Blip(httpx.ReadError):
    """A connection error whose str() is empty — what libgen.li actually threw.

    Reproducing the empty message matters: it is the reason the existing log
    line is unreadable, so a test that raises an exception WITH a message would
    pass while the real failure stayed undiagnosable.
    """

    def __init__(self):
        super().__init__("")


# --- retry ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_retries_a_transient_failure():
    """One dropped connection must not cost the whole search."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Blip()
        return httpx.Response(200, text=RESULTS_HTML)

    results = await _scraper(handler).search("dune")

    assert len(results) == 1, "a single transient blip should be retried, not returned as 0 results"
    assert results[0].title == "Dune"
    assert calls["n"] >= 2, "expected a retry after the first failure"


@pytest.mark.asyncio
async def test_search_gives_up_after_bounded_retries():
    """Retries must be bounded — a dead mirror cannot hang the request forever."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise _Blip()

    results = await _scraper(handler).search("dune")

    assert results == []
    assert calls["n"] <= 12, f"unbounded retry loop: {calls['n']} attempts"


# --- mirror fall-through --------------------------------------------------

@pytest.mark.asyncio
async def test_search_falls_through_to_the_next_mirror():
    """If the chosen mirror keeps failing, try another one before giving up.

    libgen.vg answered in 2-4s while .li was blipping, so the fallback is real
    capacity rather than a theoretical one.
    """
    seen = []

    def handler(request):
        host = request.url.host
        seen.append(host)
        if host == "libgen.li":
            raise _Blip()
        return httpx.Response(200, text=RESULTS_HTML)

    results = await _scraper(handler).search("dune")

    assert len(results) == 1, "should have fallen through to a working mirror"
    assert any(h != "libgen.li" for h in seen), f"never tried another mirror: {seen}"


# --- an empty result set is not a failure ---------------------------------

@pytest.mark.asyncio
async def test_no_matches_is_not_retried_as_an_error():
    """A genuine 'no such book' must return [] promptly, not burn retries."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=EMPTY_HTML)

    results = await _scraper(handler).search("no-such-book-xyzzy")

    assert results == []
    assert calls["n"] == 1, "an empty-but-valid page is an answer, not a failure to retry"


# --- diagnosability -------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_log_names_the_exception_type(caplog):
    """`LibGen search failed:` with nothing after it is not a usable log line."""
    def handler(request):
        raise _Blip()

    with caplog.at_level(logging.WARNING, logger="backend.libgen"):
        await _scraper(handler).search("dune")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "ReadError" in logged or "_Blip" in logged, (
        f"log must name the exception type when str(e) is empty; got: {logged!r}"
    )
