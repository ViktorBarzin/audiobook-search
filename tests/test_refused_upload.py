"""An upload Calibre-Web refuses is a failure, not "Added to Calibre".

Found 2026-09-24 while testing the ingest-race fix: a Moby-Dick PDF from libgen
starts with three stray bytes before its %PDF header. PDF readers do not mind,
but Calibre-Web sniffs uploads with libmagic, logged "Mimetype
'application/octet-stream' not found in allowed types" and answered
{"location": "/"} instead of {"location": "/tasks"}. book-search logged that as
"unexpected response", polled OPDS for a book that was never queued, and the job
reported "Added to Calibre" for a book that was not there.
"""

import httpx

import backend.main as bs_main

PDF_WITH_STRAY_BYTES = b"u\xabZ%PDF-1.4\n" + b"x" * 50_000


def _calibre_web(upload_location):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload":
            return httpx.Response(200, json={"location": upload_location})
        return httpx.Response(200, text='<input name="csrf_token" value="tok">')
    return handler


def _patch_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def client(*a, **k):
        k.pop("transport", None)
        return real(*a, transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr(bs_main.httpx, "AsyncClient", client)

    async def logged_in(c):
        return True

    monkeypatch.setattr(bs_main, "_cwa_login", logged_in)


async def test_a_refused_upload_returns_none(monkeypatch):
    _patch_client(monkeypatch, _calibre_web("/"))

    assert await bs_main._upload_to_calibre(PDF_WITH_STRAY_BYTES, "Moby-Dick.pdf") is None


async def _noop(*a, **k):
    return None


async def test_a_refused_upload_fails_the_job_and_says_why(monkeypatch):
    class Libgen:
        async def download_file(self, md5):
            return PDF_WITH_STRAY_BYTES, "Herman Melville - Moby-Dick_ or, The Whale - libgen.li.pdf"

    class Stacks:
        called = False

        async def download_via_stacks(self, md5):
            Stacks.called = True
            return {"success": False}

    async def refused(data, filename):
        return None

    posted = []

    def record(text):
        posted.append(text)
        return _noop()

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "annas_scraper", Stacks())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", refused)
    monkeypatch.setattr(bs_main, "_post_slack", record)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    md5 = "b952ccc999dba94693da7100fe705923"
    job = {"status": "queued", "title": "Moby-Dick; or, The Whale", "author": "Herman Melville",
           "md5": md5, "message": "", "kindle_email": None}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", md5, "Moby-Dick; or, The Whale", "Herman Melville", None)

    assert job["status"] == "failed"
    assert "did not accept" in job["message"]
    assert not Stacks.called, "Stacks would only fetch the same file again"
    assert len(posted) == 1 and "Moby-Dick" in posted[0]
