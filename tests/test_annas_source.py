"""Adapting Anna's Archive results for the pipeline.

AA is unreachable from here today, so the behaviour that matters most is the
unreachable path: it must report an outage rather than an empty result, because
"no copy exists" would spend the book's single attempt.
"""

import httpx
import pytest

from backend.goodreads.annas_source import AnnasSource
from backend.goodreads.sources import SourceUnavailable

RESULTS = """<html>
<a href="/md5/b0ba70d40e6f3edc41dd32b4b1b13646">
  Neuromancer
  William Gibson
  epub, 1.2MB
</a>
</html>"""


def source_for(handler, domain="annas-archive.gd"):
    src = AnnasSource()
    src.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    src._domain = domain
    return src


async def test_parses_a_result_into_a_candidate():
    async with httpx.AsyncClient() as _:
        src = source_for(lambda r: httpx.Response(200, text=RESULTS))
        results = await src.search_candidates("neuromancer gibson")

    assert len(results) == 1
    got = results[0]
    assert got.md5 == "b0ba70d40e6f3edc41dd32b4b1b13646"
    assert got.source == "annas"
    assert got.ext == "epub"
    # The query pins lang=en, which is the only language signal AA search gives.
    assert got.language == "English"


async def test_a_blocked_response_is_an_outage_not_an_empty_result():
    src = source_for(lambda r: httpx.Response(403, text="<title>DDoS-Guard</title>"))

    with pytest.raises(SourceUnavailable):
        await src.search_candidates("neuromancer")


async def test_no_reachable_domain_is_an_outage():
    src = AnnasSource()
    src._domain = None
    src._domain_checked = True

    with pytest.raises(SourceUnavailable):
        await src.search_candidates("neuromancer")


async def test_results_without_an_md5_are_ignored():
    src = source_for(lambda r: httpx.Response(200, text="<html><a href='/about'>x</a></html>"))

    assert await src.search_candidates("neuromancer") == []
