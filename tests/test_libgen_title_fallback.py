"""An md5 libgen never mirrored should still find the book by name.

Live 2026-09-04: Obviously Awesome (April Dunford) was shared from Anna's
Archive as md5 5b6e6e722084ab2d8fdef68a30fe132b. libgen's ads.php has no keyed
link for that hash and its md5 search returns nothing, because the FILE is
AA-only. The BOOK is on libgen a dozen times over under other hashes, so the
job failed with a book that was sitting right there.

Anna's Archive is 403 behind DDoS-Guard from this network, so the title cannot
come from its detail page. It has to come from the caller, which is why
/api/download-url now accepts one.

The match is title AND author via the existing matcher, never a fuzzy
last-word search: that is what once shelved a CISSP study guide as Neuromancer
because both list a Gibson.
"""

import hashlib

import pytest

from backend.goodreads.matcher import Candidate

EBOOK = b"PK\x03\x04" + b"x" * 40_000
EBOOK_MD5 = hashlib.md5(EBOOK).hexdigest()

AA_ONLY_MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"


class FakeLibGen:
    """Behaves like the live mirror did: nothing for the AA md5, the book by name."""

    def __init__(self, candidates, downloadable):
        self.candidates = candidates
        self.downloadable = downloadable
        self.queries = []
        self.download_calls = []

    async def download_file(self, md5):
        self.download_calls.append(md5)
        if md5 in self.downloadable:
            return self.downloadable[md5], "Obviously Awesome.epub"
        return None, None

    async def search_candidates(self, query):
        self.queries.append(query)
        return self.candidates


def candidate(md5, title, author, ext="epub", size=2_000_000):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language="English", size_bytes=size, source="libgen")


@pytest.fixture
def fallback():
    from backend.main import _libgen_by_title
    return _libgen_by_title


async def test_finds_the_book_under_a_different_md5(fallback, monkeypatch):
    fake = FakeLibGen(
        candidates=[candidate(EBOOK_MD5, "Obviously Awesome", "April Dunford")],
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, filename = await fallback("Obviously Awesome", "April Dunford")

    assert data == EBOOK
    assert filename == "Obviously Awesome.epub"
    assert fake.download_calls == [EBOOK_MD5]


async def test_a_wrong_author_is_not_accepted(fallback, monkeypatch):
    """The failure mode this guards: a same-surname book on the shelf."""
    fake = FakeLibGen(
        candidates=[candidate(EBOOK_MD5, "(ISC)2 CISSP Study Guide", "Darril Gibson")],
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, filename = await fallback("Neuromancer", "William Gibson")

    assert data is None, "a different book must not be substituted"
    assert filename is None
    assert fake.download_calls == []


async def test_no_title_means_no_fallback(fallback, monkeypatch):
    """Without AA metadata the title is the literal string 'Unknown'."""
    fake = FakeLibGen(candidates=[], downloadable={})
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    for bad_title in ("Unknown", "", None):
        data, _ = await fallback(bad_title, "Unknown Author")
        assert data is None
    assert fake.queries == [], "a placeholder title must not reach the mirror"


async def test_searches_on_title_and_author(fallback, monkeypatch):
    fake = FakeLibGen(candidates=[], downloadable={})
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    await fallback("Obviously Awesome", "April Dunford")

    assert fake.queries == ["Obviously Awesome April Dunford"]


async def test_nothing_found_is_not_an_error(fallback, monkeypatch):
    fake = FakeLibGen(candidates=[], downloadable={})
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, filename = await fallback("Obviously Awesome", "April Dunford")

    assert data is None and filename is None


async def test_a_mirror_outage_is_not_an_error(fallback, monkeypatch):
    """SourceUnavailable is how the pipeline path reports an outage."""
    from backend.goodreads.sources import SourceUnavailable

    class Broken(FakeLibGen):
        async def search_candidates(self, query):
            raise SourceUnavailable("every source is unreachable")

    monkeypatch.setattr("backend.main.libgen_scraper", Broken([], {}))

    data, filename = await fallback("Obviously Awesome", "April Dunford")

    assert data is None and filename is None


# --- the wiring: the AA-only md5 that started this ------------------------


async def test_try_direct_download_recovers_an_aa_only_md5(monkeypatch):
    """End to end through the path the shortcut takes.

    libgen has nothing for the AA hash, the book is there under another one,
    and the job should reach Calibre instead of failing.
    """
    import backend.main as bs_main

    fake = FakeLibGen(
        candidates=[candidate(EBOOK_MD5, "Obviously Awesome", "April Dunford")],
        downloadable={EBOOK_MD5: EBOOK},
    )
    uploaded = {}

    async def fake_upload(data, filename):
        uploaded["filename"] = filename
        uploaded["bytes"] = len(data)
        return 777

    monkeypatch.setattr(bs_main, "libgen_scraper", fake)
    monkeypatch.setattr(bs_main, "_upload_to_calibre", fake_upload)

    job = {}
    ok = await bs_main._try_direct_download(
        "j-aa", job, AA_ONLY_MD5, "Obviously Awesome", "April Dunford", None,
    )

    assert ok is True
    assert fake.download_calls == [AA_ONLY_MD5, EBOOK_MD5], \
        "the exact hash is tried first, then the title match"
    assert job["book_id"] == 777
    assert uploaded["bytes"] == len(EBOOK)


async def test_the_failure_message_names_the_missing_title():
    from backend.main import _no_route_message

    msg = _no_route_message(AA_ONLY_MD5, "Unknown")

    assert AA_ONLY_MD5 in msg
    assert "title" in msg.lower(), "it has to say what would unblock it"
    assert "No mirrors found" not in msg


async def test_the_failure_message_names_the_book_when_known():
    from backend.main import _no_route_message

    msg = _no_route_message(AA_ONLY_MD5, "Obviously Awesome", upstream="No mirrors found")

    assert "Obviously Awesome" in msg
    assert AA_ONLY_MD5 in msg
