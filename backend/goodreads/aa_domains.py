"""Discover Anna's Archive's current domains instead of hardcoding one.

Anna's Archive rotates domains, so a constant in the source goes stale silently —
`annas-archive.gl` was still hardcoded here long after it stopped answering. The
Wikipedia article tracks the live domains, so it is used as the list and each
candidate is then probed, since being listed is not the same as being reachable.

Wikimedia returns 403 to spoofed browser agents from datacentre IPs; a
descriptive User-Agent gets 200, so requests here identify themselves honestly.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_UA = "book-search/1.0 (https://viktorbarzin.me; me@viktorbarzin.me)"

# Consulted only if Wikipedia is unreachable; the discovered list wins otherwise.
FALLBACK_DOMAINS = ("annas-archive.org", "annas-archive.se", "annas-archive.li")

_DOMAIN_RE = re.compile(r"annas-archive\.[a-z]{2,6}\b", re.IGNORECASE)

CACHE_TTL_S = 24 * 3600
_cache: tuple[float, str | None] = (0.0, None)


async def list_domains(client: httpx.AsyncClient) -> list[str]:
    """Domains Wikipedia currently lists for Anna's Archive, best-effort."""
    try:
        response = await client.get(
            WIKIPEDIA_API,
            params={
                "action": "parse", "page": "Anna's Archive",
                "prop": "wikitext", "format": "json",
            },
            headers={"User-Agent": WIKIPEDIA_UA},
            timeout=30,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Could not read AA domains from Wikipedia: %s", exc)
        return list(FALLBACK_DOMAINS)

    found = {m.group(0).lower() for m in _DOMAIN_RE.finditer(response.text)}
    ordered = sorted(found) + [d for d in FALLBACK_DOMAINS if d not in found]
    logger.info("AA domains from Wikipedia: %s", ", ".join(ordered) or "none")
    return ordered


async def probe(client: httpx.AsyncClient, domain: str) -> bool:
    """Does this domain actually serve search results to us?

    A 200 is not sufficient: DDoS-Guard answers its interstitial with 200 in some
    variants, and one mirror serves a placeholder page. Requiring a real result
    link is what distinguishes a working domain from a reachable one.
    """
    try:
        response = await client.get(
            f"https://{domain}/search?q=dune&ext=epub&lang=en", timeout=30,
        )
    except Exception:
        return False
    if response.status_code != 200:
        return False
    return bool(re.search(r"/md5/[a-f0-9]{32}", response.text))


async def working_domain(client: httpx.AsyncClient, force: bool = False) -> str | None:
    """Return a domain that serves results, or None if none currently do.

    None is a normal answer, not an error: Anna's Archive has been unreachable
    from this network for months, and the pipeline is expected to run without it.
    """
    global _cache
    cached_at, cached = _cache
    if not force and cached and (time.monotonic() - cached_at) < CACHE_TTL_S:
        return cached

    for domain in await list_domains(client):
        if await probe(client, domain):
            logger.info("Anna's Archive reachable at %s", domain)
            _cache = (time.monotonic(), domain)
            return domain

    logger.info("No Anna's Archive domain is reachable; continuing without it")
    _cache = (time.monotonic(), None)
    return None
