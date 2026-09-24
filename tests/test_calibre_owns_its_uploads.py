"""Calibre-Web's own uploads belong to Calibre until it has imported them.

Live 2026-09-24, Remember Me (a 937 KB PDF): the shortcut job uploaded the book
through Calibre-Web, which drops it into the ingest folder as
new_1_20260924_060205_035606_Remember_MeSophie_Kinsella113302564_libgen.li.pdf and
converts it from there. The importer took 2m10s to start and then began a
PDF->EPUB conversion. The job waited ~170s for an id, gave up, and its end-of-job
cleanup deleted every new ebook file in the folder, including that one. The
conversion died with FileNotFoundError, the book never reached the library, and
the Kindle send was skipped ("No Calibre id"). The Backman collection missed the
same fate by seventeen seconds half an hour later.

Two rules come out of it: the cleanup leaves Calibre's uploads to Calibre, and the
id lookup keeps waiting while Calibre still has an upload in the folder.
"""

import pytest

import backend.main as bs_main

CWA_UPLOAD = "new_1_20260924_060205_035606_Remember_MeSophie_Kinsella113302564_libgen.li.pdf"
CWA_FORMAT_UPLOAD = "format_511_20260924_060205_035606_Remember_Me.epub"


def _write(path, content=b"x" * 100_000):
    path.write_bytes(content)


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture
def fast_lookup(monkeypatch):
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    monkeypatch.setattr(bs_main, "CALIBRE_ID_ATTEMPTS", 3)
    monkeypatch.setattr(bs_main, "CALIBRE_IMPORT_MAX_WAIT", 60)


def test_cleanup_leaves_a_calibre_upload_to_calibre(ingest):
    _write(ingest / CWA_UPLOAD)
    _write(ingest / "stray.epub")

    removed = bs_main._cleanup_unconsumed_ingest_files("job", pre_existing=set())

    assert removed == ["stray.epub"]
    assert (ingest / CWA_UPLOAD).exists(), "Calibre was still importing this"


def test_cleanup_leaves_a_new_format_upload_alone(ingest):
    _write(ingest / CWA_FORMAT_UPLOAD)

    assert bs_main._cleanup_unconsumed_ingest_files("job", pre_existing=set()) == []
    assert (ingest / CWA_FORMAT_UPLOAD).exists()


def test_a_calibre_upload_is_never_taken_for_a_download(ingest):
    """The Stacks "stuck file" path re-uploads and deletes whatever this returns."""
    _write(ingest / CWA_UPLOAD)
    _write(ingest / "Delivered by Stacks.epub")

    assert bs_main._ingest_ebook_files() == ["Delivered by Stacks.epub"]


def test_only_calibre_named_files_count_as_its_uploads():
    assert bs_main._is_cwa_upload(CWA_UPLOAD)
    assert bs_main._is_cwa_upload(CWA_FORMAT_UPLOAD)
    assert not bs_main._is_cwa_upload("new_book.epub")
    assert not bs_main._is_cwa_upload("Remember Me.pdf")
    assert not bs_main._is_cwa_upload(CWA_UPLOAD + ".uploading")


async def test_the_id_lookup_waits_while_calibre_is_still_importing(ingest, fast_lookup, monkeypatch):
    _write(ingest / CWA_UPLOAD)
    looks = {"n": 0}

    def lookup(title, author):
        looks["n"] += 1
        if looks["n"] == 8:
            (ingest / CWA_UPLOAD).unlink()  # the import finishes
        return 511 if looks["n"] > 8 else None

    monkeypatch.setattr(bs_main, "_calibre_id_for", lookup)

    got = await bs_main._resolve_calibre_id("Remember Me", "Sophie Kinsella")

    assert got == 511
    assert looks["n"] == 9, "it must outlast CALIBRE_ID_ATTEMPTS while Calibre works"


async def test_the_id_lookup_still_gives_up_when_calibre_is_idle(ingest, fast_lookup, monkeypatch):
    looks = {"n": 0}

    def lookup(title, author):
        looks["n"] += 1
        return None

    monkeypatch.setattr(bs_main, "_calibre_id_for", lookup)

    assert await bs_main._resolve_calibre_id("Remember Me", "Sophie Kinsella") is None
    assert looks["n"] == 3, "nothing pending means the usual number of looks"


async def test_the_id_lookup_stops_at_the_deadline(ingest, fast_lookup, monkeypatch):
    _write(ingest / CWA_UPLOAD)  # an upload Calibre never finishes
    monkeypatch.setattr(bs_main, "CALIBRE_IMPORT_MAX_WAIT", 0.05)
    monkeypatch.setattr(bs_main, "_calibre_id_for", lambda t, a: None)

    assert await bs_main._resolve_calibre_id("Remember Me", "Sophie Kinsella") is None


async def test_a_slow_pdf_import_still_reaches_the_kindle(ingest, monkeypatch):
    """The 2026-09-24 timeline, end to end."""

    class FakeLibgen:
        async def download_file(self, md5):
            return b"%PDF-1.4" + b"x" * 50_000, "Remember Me{Sophie Kinsella}{113302564} libgen.li.pdf"

    async def calibre_web_upload(data, filename):
        (ingest / CWA_UPLOAD).write_bytes(data)  # what Calibre-Web's /upload does
        return -1  # the OPDS poll timed out, as it did live

    state = {"looks": 0, "imported": False, "lost": False}

    def calibre_imports_slowly(title, author):
        state["looks"] += 1
        if not state["imported"] and not (ingest / CWA_UPLOAD).exists():
            state["lost"] = True  # deleted under the importer: the import fails
        if state["lost"]:
            return None
        if state["looks"] == 20:  # well past CALIBRE_ID_ATTEMPTS
            (ingest / CWA_UPLOAD).unlink()
            state["imported"] = True
            return None
        return 511 if state["imported"] else None

    sent = {}

    async def fake_send(book_id, title, email):
        sent["args"] = (book_id, email)
        return None

    async def no_ttl(job_id):
        return None

    monkeypatch.setattr(bs_main, "libgen_scraper", FakeLibgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", calibre_web_upload)
    monkeypatch.setattr(bs_main, "_calibre_id_for", calibre_imports_slowly)
    monkeypatch.setattr(bs_main, "_send_to_kindle", fake_send)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", no_ttl)
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    md5 = "a3d2d5da468ade3e2fdb155cdd5bd70b"
    job = {"status": "queued", "title": "Remember Me", "author": "Sophie Kinsella",
           "md5": md5, "message": "", "kindle_email": "reader@kindle.com"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", md5, "Remember Me", "Sophie Kinsella", None)

    assert not state["lost"], "book-search deleted Calibre's upload before it was imported"
    assert sent.get("args") == (511, "reader@kindle.com")
    assert job["status"] == "done"
