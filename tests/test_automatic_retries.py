"""The known failures are retried by code before any person or agent sees them.

From the shares of 2026-09-24 and the plan written after them:

- Calibre-Web down is not a problem another file can fix, so it stops the job
  at once, with no further downloads.
- Calibre-Web refusing one file (a Moby-Dick PDF libmagic did not recognise)
  says nothing about the book, so the next file of the same book is tried.
- A PDF sent to a Kindle arrives as pages to pinch and pan, and two of that
  morning's three shares were PDFs. For a Kindle share, an EPUB of the same
  book in the same language is used instead when libgen has one.

Fork rotation was measured and dropped: all five live libgen forks send a
given file to the same CDN host, so switching fork cannot route around it.
"""

import hashlib

import httpx
import pytest

import backend.main as bs_main
from backend.goodreads.matcher import Candidate
from tests.fake_library import make_library

ANCA = "ancaelena98_4RMJsy@kindle.com"


def book_bytes(tag, pdf=False):
    head = b"%PDF-1.4\n" if pdf else b"PK\x03\x04"
    return head + tag.encode() * 20_000


PDF = book_bytes("remember-me-pdf", pdf=True)
EPUB = book_bytes("remember-me-epub")
EPUB2 = book_bytes("remember-me-epub-2")
MD5_PDF = hashlib.md5(PDF).hexdigest()
MD5_EPUB = hashlib.md5(EPUB).hexdigest()
MD5_EPUB2 = hashlib.md5(EPUB2).hexdigest()


def row(md5, ext, language="English", size=500_000, title="Remember Me?", author="Sophie Kinsella"):
    return Candidate(md5=md5, title=title, author=author, ext=ext,
                     language=language, size_bytes=size, source="libgen")


class Libgen:
    def __init__(self, files, rows):
        self.files, self.rows = files, rows
        self.downloads, self.queries = [], []

    async def download_file(self, md5):
        self.downloads.append(md5)
        data = self.files.get(md5)
        return (data, f"{md5}.{'pdf' if data and data.startswith(b'%PDF') else 'epub'}") if data else (None, None)

    async def search_candidates(self, query):
        self.queries.append(query)
        return list(self.rows)


async def _noop(*a, **k):
    return None


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A library that will hold Remember Me once an upload lands, and a record
    of what was uploaded and sent."""
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "ingest"))
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path / "lib", [
        (511, "Remember Me?", "Sophie Kinsella", {"EPUB": 400_000}),
    ])))
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    monkeypatch.setattr(bs_main, "_post_slack", _noop)
    record = {"uploaded": [], "sent": []}

    async def send(book_id, title, email):
        record["sent"].append(book_id)
        return None

    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    return record


def accept_uploads(monkeypatch, record, refuse=()):
    async def upload(data, filename):
        record["uploaded"].append(hashlib.md5(data).hexdigest())
        return None if hashlib.md5(data).hexdigest() in refuse else 511

    monkeypatch.setattr(bs_main, "_upload_to_calibre", upload)


def a_job(monkeypatch, kindle_email=ANCA, md5=MD5_PDF):
    job = {"status": "queued", "title": "Remember Me?", "author": "Sophie Kinsella",
           "md5": md5, "message": "", "kindle_email": kindle_email}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})
    return job


async def run(md5=MD5_PDF):
    await bs_main._process_download("j", md5, "Remember Me?", "Sophie Kinsella", None)


# --- Calibre down ---------------------------------------------------------------


def calibre_web(handler, monkeypatch):
    real = httpx.AsyncClient

    def client(*a, **k):
        k.pop("transport", None)
        return real(*a, transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr(bs_main.httpx, "AsyncClient", client)


async def test_a_calibre_that_does_not_answer_is_unreachable(monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused")

    calibre_web(down, monkeypatch)

    with pytest.raises(bs_main.CalibreUnreachable):
        await bs_main._upload_to_calibre(PDF, "x.pdf")


async def test_a_calibre_answering_5xx_is_unreachable(monkeypatch):
    calibre_web(lambda request: httpx.Response(502, text="bad gateway"), monkeypatch)

    with pytest.raises(bs_main.CalibreUnreachable):
        await bs_main._upload_to_calibre(PDF, "x.pdf")


async def test_an_accepted_upload_answers_without_polling_opds(monkeypatch):
    """The library settles the id; an OPDS poll only cost up to 60 s.

    Live on 2026-09-24 that poll held a PDF share in "Adding to Calibre" for a
    minute after Calibre had taken the file, and for The Yellow Wallpaper its
    author-first search named Bill Perkins's Die with Zero.
    """
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/upload":
            return httpx.Response(200, json={"location": "/tasks"})
        return httpx.Response(200, text='<input name="csrf_token" value="tok">')

    async def logged_in(client):
        return True

    calibre_web(handler, monkeypatch)
    monkeypatch.setattr(bs_main, "_cwa_login", logged_in)

    assert await bs_main._upload_to_calibre(EPUB, "x.epub") == -1
    assert not any(p.startswith("/opds") for p in paths)


async def test_calibre_down_ends_the_job_without_more_downloads(world, monkeypatch):
    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB}, [row(MD5_PDF, "pdf"), row(MD5_EPUB, "epub")])

    async def unreachable(data, filename):
        raise bs_main.CalibreUnreachable("login page answered 502")

    class Stacks:
        called = False

        async def download_via_stacks(self, md5):
            Stacks.called = True
            return {"success": False}

    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    monkeypatch.setattr(bs_main, "_upload_to_calibre", unreachable)
    monkeypatch.setattr(bs_main, "annas_scraper", Stacks())
    job = a_job(monkeypatch, kindle_email=None)

    await run()

    assert job["status"] == "failed"
    assert job["code"] == "calibre_down"
    assert "Calibre" in job["final_text"]
    assert len(libgen.downloads) == 1, "another file cannot help while Calibre is down"
    assert not Stacks.called


# --- the next file when one is refused -------------------------------------------


async def test_a_refused_file_makes_way_for_another_file_of_the_book(world, monkeypatch):
    libgen = Libgen({MD5_EPUB: EPUB, MD5_EPUB2: EPUB2},
                    [row(MD5_EPUB, "epub"), row(MD5_EPUB2, "epub", size=400_000)])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world, refuse={MD5_EPUB})
    job = a_job(monkeypatch, kindle_email=None, md5=MD5_EPUB)

    await run(MD5_EPUB)

    assert world["uploaded"] == [MD5_EPUB, MD5_EPUB2]
    assert job["outcome"] == "done"


async def test_when_every_file_is_refused_the_job_says_calibre_refused_it(world, monkeypatch):
    libgen = Libgen({MD5_EPUB: EPUB, MD5_EPUB2: EPUB2},
                    [row(MD5_EPUB, "epub"), row(MD5_EPUB2, "epub", size=400_000)])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world, refuse={MD5_EPUB, MD5_EPUB2})
    job = a_job(monkeypatch, kindle_email=None, md5=MD5_EPUB)

    await run(MD5_EPUB)

    assert job["status"] == "failed"
    assert job["code"] == "refused"
    assert "did not accept" in job["final_text"]
    assert sorted(world["uploaded"]) == sorted([MD5_EPUB, MD5_EPUB2])


# --- an EPUB instead of a PDF, for a Kindle ---------------------------------------


async def test_a_kindle_share_of_a_pdf_sends_an_epub_of_the_same_book(world, monkeypatch):
    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB}, [row(MD5_PDF, "pdf"), row(MD5_EPUB, "epub")])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world)
    job = a_job(monkeypatch)

    await run()

    assert world["uploaded"] == [MD5_EPUB]
    assert world["sent"] == [511]
    assert job["outcome"] == "done"
    assert "epub instead of the pdf" in job["final_text"]


async def test_a_calibre_only_share_keeps_the_pdf_that_was_picked(world, monkeypatch):
    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB}, [row(MD5_PDF, "pdf"), row(MD5_EPUB, "epub")])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world)
    a_job(monkeypatch, kindle_email=None)

    await run()

    assert world["uploaded"] == [MD5_PDF]
    assert libgen.queries == [], "no search for a swap on a Calibre-only share"


async def test_no_epub_on_libgen_keeps_the_pdf(world, monkeypatch):
    libgen = Libgen({MD5_PDF: PDF}, [row(MD5_PDF, "pdf")])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world)
    job = a_job(monkeypatch)

    await run()

    assert world["uploaded"] == [MD5_PDF]
    assert job["outcome"] == "done"


async def test_an_epub_calibre_refuses_falls_back_to_the_pdf(world, monkeypatch):
    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB}, [row(MD5_PDF, "pdf"), row(MD5_EPUB, "epub")])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world, refuse={MD5_EPUB})
    job = a_job(monkeypatch)

    await run()

    assert world["uploaded"] == [MD5_EPUB, MD5_PDF]
    assert job["outcome"] == "done"


async def test_an_epub_in_another_language_is_not_swapped_in(world, monkeypatch):
    """Anna Karenina shared in one translation must not become another language."""
    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB},
                    [row(MD5_PDF, "pdf", language="German"), row(MD5_EPUB, "epub")])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world)
    a_job(monkeypatch)

    await run()

    assert world["uploaded"] == [MD5_PDF]


async def test_a_pdf_whose_language_is_unknown_is_kept(world, monkeypatch):
    """No row names the shared file, so its language cannot be checked."""
    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB}, [row(MD5_EPUB, "epub")])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world)
    a_job(monkeypatch)

    await run()

    assert world["uploaded"] == [MD5_PDF]


async def test_an_epub_over_the_mail_limit_is_not_swapped_in(world, monkeypatch):
    from backend.kindle import MAX_BOOK_BYTES

    libgen = Libgen({MD5_PDF: PDF, MD5_EPUB: EPUB},
                    [row(MD5_PDF, "pdf"), row(MD5_EPUB, "epub", size=MAX_BOOK_BYTES + 1)])
    monkeypatch.setattr(bs_main, "libgen_scraper", libgen)
    accept_uploads(monkeypatch, world)
    a_job(monkeypatch)

    await run()

    assert world["uploaded"] == [MD5_PDF]
