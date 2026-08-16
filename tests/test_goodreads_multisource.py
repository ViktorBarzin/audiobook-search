"""Searching more than one source.

Anna's Archive indexes more than libgen, so it is worth asking — but it has been
403 behind DDoS-Guard from this network all day, direct and through the UK VPN
exit alike. The rule that matters is therefore that an unreachable source costs
nothing and changes no outcome: libgen results must still come back.
"""

import pytest

from backend.goodreads.matcher import Candidate
from backend.goodreads.sources import MultiSource, SourceUnavailable


def cand(md5, title="Neuromancer", source="libgen", ext="epub"):
    return Candidate(md5=md5, title=title, author="William Gibson", ext=ext,
                     language="English", size_bytes=500_000, source=source)


class Stub:
    def __init__(self, isbn_hits=None, text_hits=None, fail=False):
        self.isbn_hits = isbn_hits or []
        self.text_hits = text_hits or []
        self.fail = fail
        self.calls = 0

    async def search_by_isbn(self, isbn):
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("blocked")
        return list(self.isbn_hits)

    async def search_candidates(self, query):
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("blocked")
        return list(self.text_hits)


async def test_merges_results_from_both_sources():
    primary = Stub(text_hits=[cand("a" * 32)])
    secondary = Stub(text_hits=[cand("b" * 32, source="annas")])

    results = await MultiSource([primary, secondary]).search_candidates("neuromancer")

    assert {c.md5 for c in results} == {"a" * 32, "b" * 32}


async def test_an_unreachable_secondary_does_not_break_the_search():
    """The live case: AA is blocked, libgen must still deliver."""
    primary = Stub(text_hits=[cand("a" * 32)])
    blocked = Stub(fail=True)

    results = await MultiSource([primary, blocked]).search_candidates("neuromancer")

    assert [c.md5 for c in results] == ["a" * 32]


async def test_all_sources_unreachable_is_reported_as_an_outage():
    """Not 'the book does not exist' — that distinction protects the one attempt."""
    with pytest.raises(SourceUnavailable):
        await MultiSource([Stub(fail=True), Stub(fail=True)]).search_candidates("x")


async def test_duplicate_md5s_across_sources_are_collapsed():
    primary = Stub(text_hits=[cand("a" * 32)])
    secondary = Stub(text_hits=[cand("a" * 32, source="annas")])

    results = await MultiSource([primary, secondary]).search_candidates("neuromancer")

    assert len(results) == 1
    assert results[0].source == "libgen", "the fetchable source wins"


async def test_isbn_lookup_asks_every_source():
    primary = Stub(isbn_hits=[cand("a" * 32)])
    secondary = Stub(isbn_hits=[cand("b" * 32, source="annas")])

    results = await MultiSource([primary, secondary]).search_by_isbn("006343315X")

    assert len(results) == 2
    assert primary.calls == 1 and secondary.calls == 1
