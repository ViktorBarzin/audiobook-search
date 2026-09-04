"""A shortcut download that says "done" must actually reach the Kindle.

Live 2026-09-04, Obviously Awesome: the book uploaded to Calibre fine, but
_upload_to_calibre's OPDS poll returned -1 (an entry existed with no
downloadable format yet, because CWA was still importing). _maybe_send_to_kindle
bails on `bid <= 0`, so the job reported "Added to Calibre" and the Kindle got
nothing, silently. The book was in the library ten seconds later as id 507.

The Goodreads path already solves this by resolving the id from metadata.db on
title AND author. The shortcut path now uses the same resolver, and reports
honestly when it cannot.
"""

import backend.main as bs_main

EBOOK = b"PK\x03\x04" + b"x" * 50_000


class FakeLibgen:
    async def download_file(self, md5):
        return EBOOK, "Obviously Awesome.epub"


def _job(**kw):
    job = {"status": "done", "kindle_email": "reader@kindle.com", "book_id": None}
    job.update(kw)
    return job


async def test_an_unresolved_id_is_looked_up_in_the_library(monkeypatch):
    """The -1 case: upload worked, OPDS was too early, the library knows."""
    monkeypatch.setattr(bs_main, "libgen_scraper", FakeLibgen())

    async def fake_upload(data, filename):
        return -1

    monkeypatch.setattr(bs_main, "_upload_to_calibre", fake_upload)
    monkeypatch.setattr(bs_main, "_calibre_id_for", lambda t, a: 507)

    job = {}
    ok = await bs_main._try_direct_download(
        "j1", job, "a" * 32, "Obviously Awesome", "April Dunford", None,
    )

    assert ok is True
    assert job["book_id"] == 507, "the library id must replace the -1"


async def test_the_kindle_send_fires_once_the_id_is_known(monkeypatch):
    sent = {}

    async def fake_send(book_id, title, email):
        sent["args"] = (book_id, title, email)
        return None

    monkeypatch.setattr(bs_main, "_send_to_kindle", fake_send)
    job = _job(book_id=507)
    monkeypatch.setattr(bs_main, "_download_jobs", {"j2": job})

    await bs_main._maybe_send_to_kindle("j2", "Obviously Awesome")

    assert sent["args"] == (507, "Obviously Awesome", "reader@kindle.com")
    assert "sent to" in job["message"].lower()


async def test_an_unsendable_book_says_so_instead_of_claiming_done(monkeypatch):
    """Silence was the bug. An id that cannot be found has to be reported."""
    monkeypatch.setattr(bs_main, "_calibre_id_for", lambda t, a: None)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bs_main.asyncio, "sleep", no_sleep)
    job = _job(book_id=-1)
    monkeypatch.setattr(bs_main, "_download_jobs", {"j3": job})

    await bs_main._maybe_send_to_kindle("j3", "Obviously Awesome")

    assert job["status"] == "failed", "a job that did not deliver is not done"
    assert "kindle" in job["message"].lower()
    assert "identify" in job["message"].lower() or "id" in job["message"].lower()


async def test_a_late_import_is_found_on_a_retry(monkeypatch):
    """CWA committed the book ten seconds after the upload returned."""
    calls = {"n": 0}

    def late(title, author):
        calls["n"] += 1
        return 507 if calls["n"] >= 2 else None

    monkeypatch.setattr(bs_main, "_calibre_id_for", late)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bs_main.asyncio, "sleep", no_sleep)

    got = await bs_main._resolve_calibre_id("Obviously Awesome", "April Dunford")

    assert got == 507
    assert calls["n"] == 2


async def test_no_kindle_email_is_not_a_failure(monkeypatch):
    job = _job(book_id=-1, kindle_email=None)
    monkeypatch.setattr(bs_main, "_download_jobs", {"j4": job})

    await bs_main._maybe_send_to_kindle("j4", "Obviously Awesome")

    assert job["status"] == "done", "no Kindle requested means nothing to report"
