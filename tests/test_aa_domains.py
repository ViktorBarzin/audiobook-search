"""Tests for Anna's Archive domain discovery.

The hardcoded domain went stale without anyone noticing, so the list comes from
Wikipedia — but being listed is not the same as being reachable: as of 2026-08-16
every listed domain answers with a DDoS-Guard interstitial from this network.
"""

import httpx

from backend.goodreads import aa_domains
from backend.goodreads.aa_domains import FALLBACK_DOMAINS, list_domains, probe, working_domain

WIKITEXT = """{"parse": {"wikitext": {"*": "Anna's Archive mirrors include
https://annas-archive.gd and https://annas-archive.pk and annas-archive.li ."}}}"""

RESULTS_PAGE = '<html><a href="/md5/b0ba70d40e6f3edc41dd32b4b1b13646">Dune</a></html>'
DDOS_GUARD_PAGE = "<html><title>DDoS-Guard</title>Checking your browser</html>"


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_reads_domains_from_wikipedia():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text=WIKITEXT)

    async with client_for(handler) as client:
        domains = await list_domains(client)

    assert "annas-archive.gd" in domains
    assert "annas-archive.pk" in domains
    assert "book-search" in seen["ua"], "Wikimedia 403s spoofed browser agents"


async def test_falls_back_when_wikipedia_is_unreachable():
    async with client_for(lambda r: httpx.Response(503)) as client:
        assert await list_domains(client) == list(FALLBACK_DOMAINS)


async def test_probe_rejects_an_interstitial_served_with_200():
    async with client_for(lambda r: httpx.Response(200, text=DDOS_GUARD_PAGE)) as client:
        assert await probe(client, "annas-archive.gl") is False


async def test_probe_accepts_a_page_with_real_results():
    async with client_for(lambda r: httpx.Response(200, text=RESULTS_PAGE)) as client:
        assert await probe(client, "annas-archive.gd") is True


async def test_working_domain_returns_none_when_all_are_blocked():
    """The current reality — the pipeline must run happily without AA."""
    def handler(request):
        if "wikipedia" in request.url.host:
            return httpx.Response(200, text=WIKITEXT)
        return httpx.Response(403, text=DDOS_GUARD_PAGE)

    aa_domains._cache = (0.0, None)
    async with client_for(handler) as client:
        assert await working_domain(client, force=True) is None


async def test_working_domain_picks_the_first_that_serves_results():
    def handler(request):
        if "wikipedia" in request.url.host:
            return httpx.Response(200, text=WIKITEXT)
        if request.url.host == "annas-archive.pk":
            return httpx.Response(200, text=RESULTS_PAGE)
        return httpx.Response(403, text=DDOS_GUARD_PAGE)

    aa_domains._cache = (0.0, None)
    async with client_for(handler) as client:
        assert await working_domain(client, force=True) == "annas-archive.pk"
