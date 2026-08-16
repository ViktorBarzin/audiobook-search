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


async def test_the_job_finishes_rather_than_sitting_on_downloading(monkeypatch):
    """An iOS Shortcut polls for 'done'; leaving it on 'downloading' looks stuck."""
    libgen = FakeLibgen()

    async def fake_upload(data, filename):
        return 505

    class NoStacks:
        async def download_via_stacks(self, md5):
            return {"success": False, "error": "stacks unavailable"}

    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    monkeypatch.setattr(bs_main, "annas_scraper", NoStacks())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", fake_upload)
    async def no_kindle(*a, **k):
        return None

    monkeypatch.setattr(bs_main, "_maybe_send_to_kindle", no_kindle)

    job = {"status": "queued", "title": "Unknown", "author": "Unknown Author",
           "md5": "a" * 32, "message": "", "kindle_email": None}
    bs_main._download_jobs["j9"] = job

    await bs_main._process_download("j9", "a" * 32, "Unknown", "Unknown Author", None)

    assert job["status"] == "done", f"job ended as {job['status']!r}"
    assert job.get("book_id") == 505


async def test_libgen_is_tried_before_stacks(monkeypatch):
    """Stacks accepts an md5 and then never delivers, because it fetches from AA
    — which is blocked. Trying it first left the job hanging on a route that
    cannot work while libgen had the file all along."""
    order = []
    libgen = FakeLibgen()

    class Stacks:
        async def download_via_stacks(self, md5):
            order.append("stacks")
            return {"success": True, "message": "queued"}

    class Watched(FakeLibgen):
        async def download_file(self, md5):
            order.append("libgen")
            return await FakeLibgen.download_file(self, md5)

    async def fake_upload(data, filename):
        return 506

    async def no_kindle(*a, **k):
        return None

    monkeypatch.setattr(bs_main, "libgen_scraper", Watched())
    monkeypatch.setattr(bs_main, "annas_scraper", Stacks())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", fake_upload)
    monkeypatch.setattr(bs_main, "_maybe_send_to_kindle", no_kindle)

    job = {"status": "queued", "md5": "e" * 32, "message": "", "kindle_email": None}
    bs_main._download_jobs["j10"] = job
    await bs_main._process_download("j10", "e" * 32, "Unknown", "Unknown Author", None)

    assert order and order[0] == "libgen", f"order was {order}"
    assert job["status"] == "done"
    assert job.get("book_id") == 506


async def test_the_endpoint_does_not_wait_on_annas_archive(monkeypatch):
    """Sharing a link from a phone should feel instant.

    The endpoint asked Anna's Archive for the title first; AA is blocked, so it
    burned a 60s FlareSolverr timeout before the job even started. Metadata is a
    nicety here — Calibre reads the real title out of the file.
    """
    import asyncio as _asyncio

    class SlowAA:
        async def get_detail(self, md5):
            await _asyncio.sleep(30)
            return None

    monkeypatch.setattr(bs_main, "annas_scraper", SlowAA())
    monkeypatch.setattr(bs_main, "ANNAS_DETAIL_TIMEOUT", 0.2)

    detail = await bs_main._detail_best_effort("a" * 32)
    assert detail is None
