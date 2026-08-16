"""Fetching a book from just an md5.

Viktor's manual route: Anna's Archive can only be searched by a human, so he
finds a book there on his phone and shares its link. The link carries an md5 and
nothing else usable — AA's own detail page is unreachable from here — and libgen
serves files by md5 even when its search does not index them. So an md5 alone
has to be enough.

Before this, _try_direct_download bailed out at `if not detail` and the job died
with "All download methods failed" while that same md5 downloaded fine.
"""

import backend.main as bs_main


class FakeLibgen:
    def __init__(self, data=b"PK\x03\x04" + b"x" * 50_000, filename="Book - Author.epub"):
        self.data, self.filename, self.calls = data, filename, []

    async def download_file(self, md5):
        self.calls.append(md5)
        return self.data, self.filename


async def test_falls_back_to_libgen_when_there_is_no_detail_page(monkeypatch):
    libgen = FakeLibgen()
    uploaded = {}

    async def fake_upload(data, filename):
        uploaded["filename"] = filename
        return 601

    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    monkeypatch.setattr(bs_main, "_upload_to_calibre", fake_upload)

    job = {}
    ok = await bs_main._try_direct_download("j1", job, "a" * 32, "Unknown", "Unknown Author", None)

    assert ok is True, "an md5 alone must be enough"
    assert libgen.calls == ["a" * 32]
    assert job.get("book_id") == 601


async def test_reports_failure_when_libgen_has_nothing(monkeypatch):
    class Empty(FakeLibgen):
        async def download_file(self, md5):
            self.calls.append(md5)
            return None, None

    monkeypatch.setattr(bs_main, "libgen_scraper", Empty())
    assert await bs_main._try_direct_download("j2", {}, "b" * 32, "T", "A", None) is False


async def test_no_libgen_scraper_configured_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(bs_main, "libgen_scraper", None)
    assert await bs_main._try_direct_download("j3", {}, "c" * 32, "T", "A", None) is False


async def test_a_tiny_file_is_refused(monkeypatch):
    """A 154-byte rate-limit stub was once imported as a real book."""
    monkeypatch.setattr(bs_main, "libgen_scraper", FakeLibgen(data=b"tiny"))
    assert await bs_main._try_direct_download("j4", {}, "d" * 32, "T", "A", None) is False
