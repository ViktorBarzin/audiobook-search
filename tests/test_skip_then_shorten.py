"""Excluding the failed hash and shortening the query have to compose.

Live 2026-09-07, third attempt at The Mom Test. One search request, then:

    GET libgen.li/index.php?req=The+Mom+Test%3A+how+to+talk+to+customers+and
        +learn+if+your+business+is+a+good+idea+when+everybody+is+lying+to+you
        +Rob+Fitzpatrick  200 OK
    No confident libgen match for 'The Mom Test: ...' by 'Rob Fitzpatrick'
      (0 candidates)

The full-title query returned exactly one row: the hash the job came in with.
Rows had arrived, so the query loop stopped, and the filter that drops that hash
then emptied them. The shorter query, which finds this book five times over on
the same mirror, was never sent.

So the hash has to be excluded before a query counts as answered.
"""

import hashlib

import pytest

from backend.goodreads.matcher import Candidate

EBOOK = b"PK\x03\x04" + b"q" * 150_000
EBOOK_MD5 = hashlib.md5(EBOOK).hexdigest()

SHARED_MD5 = "14c3eabe5771f3bc439c71f0830f478f"
LONG_TITLE = ("The Mom Test: how to talk to customers and learn if your business "
              "is a good idea when everybody is lying to you")
AUTHOR = "Rob Fitzpatrick"


class MirrorPerQuery:
    def __init__(self, answers, downloadable):
        self.answers = answers
        self.downloadable = downloadable
        self.queries = []
        self.download_calls = []

    async def search_candidates(self, query):
        self.queries.append(query)
        return self.answers.get(query, [])

    async def download_file(self, md5):
        self.download_calls.append(md5)
        if md5 in self.downloadable:
            return self.downloadable[md5], "The Mom Test.epub"
        return None, None


def candidate(md5, title=LONG_TITLE, author=AUTHOR, ext="epub", size=1_500_000):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language="English", size_bytes=size, source="libgen")


@pytest.fixture
def fallback():
    from backend.main import _libgen_by_title
    return _libgen_by_title


async def test_a_search_returning_only_the_skipped_hash_is_not_an_answer(
    fallback, monkeypatch,
):
    fake = MirrorPerQuery(
        answers={
            f"{LONG_TITLE} {AUTHOR}": [candidate(SHARED_MD5)],
            f"The Mom Test {AUTHOR}": [
                candidate(SHARED_MD5),
                candidate(EBOOK_MD5, title=LONG_TITLE + " v1.04 b l 1641747"),
            ],
        },
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(LONG_TITLE, AUTHOR, skip_md5=SHARED_MD5)

    assert data == EBOOK, f"tried {fake.queries}"
    assert len(fake.queries) >= 2, "the shorter query should have been sent"
    assert SHARED_MD5 not in fake.download_calls


async def test_every_query_returning_only_that_hash_ends_quietly(fallback, monkeypatch):
    fake = MirrorPerQuery(
        answers={
            f"{LONG_TITLE} {AUTHOR}": [candidate(SHARED_MD5)],
            f"The Mom Test {AUTHOR}": [candidate(SHARED_MD5)],
            "The Mom Test": [candidate(SHARED_MD5)],
        },
        downloadable={SHARED_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(LONG_TITLE, AUTHOR, skip_md5=SHARED_MD5)

    assert data is None
    assert fake.download_calls == [], "the hash that already failed stays excluded"
