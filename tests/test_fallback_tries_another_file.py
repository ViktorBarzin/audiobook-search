"""The title fallback exists to find a DIFFERENT file, and to keep going.

Live 2026-09-07, second attempt at The Mom Test. The search fix worked and the
matcher agreed:

    Falling back to libgen md5 14c3eabe5771f3bc439c71f0830f478f for
      'The Mom Test: ...' by 'Rob Fitzpatrick' (matched on title_author)
    LibGen download failed: Server error '503 Service Unavailable' for
      https://cdn3.booksdl.lc/get.php?md5=14c3eabe...

It chose the md5 the job had already failed on three times, and its CDN went on
answering 503 and 520. libgen has this book under at least four other hashes.

So two things. The hash we came in with is excluded, since the whole purpose of
this route is a file libgen serves when that one does not. And one candidate is
not the end: a mirror that fails on one file is common enough that giving up
after the first is what makes the flow feel unreliable.

Identity is untouched. Every attempt goes through the same matcher, so the
second choice is as strictly checked as the first.
"""

import hashlib

import pytest

from backend.goodreads.matcher import Candidate

EBOOK = b"PK\x03\x04" + b"z" * 120_000
EBOOK_MD5 = hashlib.md5(EBOOK).hexdigest()

SHARED_MD5 = "14c3eabe5771f3bc439c71f0830f478f"
TITLE = "The Mom Test"
AUTHOR = "Rob Fitzpatrick"


class Mirror:
    """Serves one file and 503s the rest, the way libgen behaved."""

    def __init__(self, candidates, downloadable):
        self.candidates = candidates
        self.downloadable = downloadable
        self.download_calls = []

    async def search_candidates(self, query):
        return self.candidates

    async def download_file(self, md5):
        self.download_calls.append(md5)
        if md5 in self.downloadable:
            return self.downloadable[md5], f"{TITLE}.epub"
        return None, None


def candidate(md5, title=TITLE, author=AUTHOR, ext="epub", size=1_500_000):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language="English", size_bytes=size, source="libgen")


@pytest.fixture
def fallback():
    from backend.main import _libgen_by_title
    return _libgen_by_title


async def test_the_hash_we_came_in_with_is_not_chosen(fallback, monkeypatch):
    fake = Mirror(
        candidates=[candidate(SHARED_MD5), candidate(EBOOK_MD5)],
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(TITLE, AUTHOR, skip_md5=SHARED_MD5)

    assert data == EBOOK
    assert SHARED_MD5 not in fake.download_calls, "that hash already failed"


async def test_a_failed_download_moves_on_to_the_next_file(fallback, monkeypatch):
    dead = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    fake = Mirror(
        candidates=[candidate(dead), candidate(EBOOK_MD5)],
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(TITLE, AUTHOR)

    assert data == EBOOK
    assert fake.download_calls == [dead, EBOOK_MD5]


async def test_it_gives_up_rather_than_grinding_through_every_row(fallback, monkeypatch):
    """A mirror having a bad day should not turn into 25 requests."""
    fake = Mirror(
        candidates=[candidate(f"{i:032x}") for i in range(25)],
        downloadable={},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(TITLE, AUTHOR)

    assert data is None
    assert len(fake.download_calls) <= 4, f"tried {len(fake.download_calls)} files"


async def test_retrying_does_not_loosen_identity(fallback, monkeypatch):
    """The second choice is checked as strictly as the first."""
    fake = Mirror(
        candidates=[candidate("b" * 32), candidate(EBOOK_MD5, title="Dune Messiah",
                                                   author="Frank Herbert")],
        downloadable={EBOOK_MD5: EBOOK},
    )
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(TITLE, AUTHOR)

    assert data is None, "a different book must not be substituted on a retry"
    assert EBOOK_MD5 not in fake.download_calls


async def test_skip_md5_is_optional(fallback, monkeypatch):
    """Callers with no hash to exclude keep working unchanged."""
    fake = Mirror(candidates=[candidate(EBOOK_MD5)], downloadable={EBOOK_MD5: EBOOK})
    monkeypatch.setattr("backend.main.libgen_scraper", fake)

    data, _ = await fallback(TITLE, AUTHOR)

    assert data == EBOOK
