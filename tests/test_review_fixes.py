"""Fixes from an independent review of the share-outcome change, 2026-09-24.

Each test here reproduces a case the reviewer confirmed against the first
version: two different books held back as one by the send guard, a title-only
share confirmed against another author's book, a doubled answer at shutdown,
and a few ways a share could still end without a word.
"""

import asyncio
import io
import sqlite3
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper
from tests.fake_library import make_library

ANCA = "ancaelena98_4RMJsy@kindle.com"
SMOKE = "spam@viktorbarzin.me"
MD5 = "d7191a16e9bc05c7afcf0a0c53600089"


async def _noop(*a, **k):
    return None


@pytest.fixture
def slack(monkeypatch):
    posted = []

    async def record(text):
        posted.append(text)

    monkeypatch.setattr(bs_main, "_post_slack", record)
    return posted


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(bs_main, "SMTP_USER", "calibre-web@viktorbarzin.me")
    monkeypatch.setattr(bs_main, "SMTP_PASS", "secret")
    sent = []
    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", lambda msg: sent.append(msg["Subject"]))
    real = httpx.AsyncClient

    def opds(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"B" * 5000)),
                    follow_redirects=True)

    monkeypatch.setattr(bs_main.httpx, "AsyncClient", opds)
    return sent


# --- 1. the send guard tells books apart ------------------------------------------


async def test_two_bulgarian_books_are_two_books(tmp_path, monkeypatch, smtp):
    """normalize_title folds Cyrillic away, so these once shared one key."""
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path, [
        (601, "Под игото", "Иван Вазов", {"EPUB": 300_000}),
        (602, "Тютюн", "Димитър Димов", {"EPUB": 300_000}),
    ])))
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})

    assert await bs_main._send_to_kindle(601, "Под игото", ANCA) is None
    assert await bs_main._send_to_kindle(602, "Тютюн", ANCA) is None
    assert len(smtp) == 2


async def test_books_that_differ_only_in_the_subtitle_are_two_books(tmp_path, monkeypatch, smtp):
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path, [
        (603, "Sapiens: A Brief History of Humankind", "Yuval Noah Harari", {"EPUB": 1}),
        (604, "Sapiens: A Graphic History", "Yuval Noah Harari", {"EPUB": 1}),
    ])))

    assert await bs_main._send_to_kindle(603, "Sapiens", ANCA) is None
    assert await bs_main._send_to_kindle(604, "Sapiens", ANCA) is None
    assert len(smtp) == 2


async def test_the_same_library_row_is_held_even_when_its_title_cannot_be_read(
    tmp_path, monkeypatch, smtp,
):
    """With the library unreadable the title key changes; the id key still holds."""
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(tmp_path / "missing"))

    assert await bs_main._send_to_kindle(605, "A Title", ANCA) is None
    assert isinstance(await bs_main._send_to_kindle(605, "Another Title", ANCA), bs_main.AlreadySent)


# --- 2. a title-only share cannot land on another author's book --------------------


def _epub(title, author):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as epub:
        epub.writestr("META-INF/container.xml",
                      '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                      '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        epub.writestr("content.opf",
                      '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                      'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                      f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
                      "</metadata></package>")
    return buffer.getvalue() + b"\0" * 6000


@pytest.fixture
def principles(tmp_path, monkeypatch, slack):
    """The library holds Carnap's Principles and another Dalio book; the upload
    adds Dalio's Principles, as Calibre would."""
    lib = make_library(tmp_path, [
        (250, "Big Debt Crises", "Ray Dalio", {"EPUB": 1}),
        (300, "Principles", "Rudolf Carnap", {"PDF": 1}),
    ])
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(lib))
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "ingest"))
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    monkeypatch.setattr(bs_main, "CALIBRE_ID_ATTEMPTS", 2)
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)

    def import_dalio():
        conn = sqlite3.connect(lib / "metadata.db")
        conn.execute("INSERT INTO books VALUES (512, 'Principles', '2026-09-24 09:00:00+00:00')")
        conn.execute("INSERT INTO authors VALUES (512, 'Ray Dalio')")
        conn.execute("INSERT INTO books_authors_link VALUES (512, 512)")
        conn.commit()
        conn.close()

    sends = []

    async def send(book_id, title, email):
        sends.append(book_id)
        return None

    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    return import_dalio, sends


@pytest.mark.parametrize("opds_hint", [-1, 300, 250])
async def test_a_title_only_share_is_not_confirmed_against_another_author(principles, monkeypatch, opds_hint):
    import_dalio, sends = principles
    book = _epub("Principles", "Ray Dalio")

    class Libgen:
        async def download_file(self, md5):
            return book, "Ray Dalio - Principles - libgen.li.epub"

    async def upload(data, filename):
        import_dalio()
        return opds_hint

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", upload)
    job = {"status": "queued", "title": "Principles", "author": "Unknown Author",
           "md5": MD5, "message": "", "kindle_email": ANCA}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", MD5, "Principles", "Unknown Author", None)

    assert sends == [512]


async def test_a_file_with_no_title_anywhere_ignores_an_old_opds_hint(principles, monkeypatch):
    """An author-first OPDS search right after the upload names an older book."""
    import_dalio, sends = principles

    class Libgen:
        async def download_file(self, md5):
            return b"%PDF-1.4" + b"x" * 50_000, "unnamed.pdf"

    async def upload(data, filename):
        return 250

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", upload)
    job = {"status": "queued", "title": "Unknown", "author": "Unknown Author",
           "md5": MD5, "message": "", "kindle_email": ANCA}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", MD5, "Unknown", "Unknown Author", None)

    assert sends == [], "book 250 predates the upload, so it is not the book fetched"
    assert job["outcome"] == "failed"


# --- 3. shutdown does not answer twice ----------------------------------------------


async def test_a_job_already_reported_lost_is_not_settled_again(slack, monkeypatch):
    job = {"status": "done", "title": "Moby-Dick", "md5": "b" * 32, "kindle_email": None,
           "book_id": 5, "finished": True, "outcome": "lost", "final_text": "lost"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._settle_job("j")

    assert slack == []
    assert job["outcome"] == "lost"


async def test_shutdown_leaves_a_job_that_is_sending_its_email(slack, monkeypatch, state_dir):
    """It will most likely finish; if it does not, its journal entry reports it."""
    sending = {"status": "done", "phase": "sending", "title": "Dune", "md5": "c" * 32,
               "kindle_email": ANCA, "finished": False}
    monkeypatch.setattr(bs_main, "_download_jobs", {"s": sending})
    bs_main._journal_write("s", sending)

    await bs_main._report_jobs_lost_on_shutdown()

    assert slack == []
    assert (state_dir / "jobs" / "s.json").exists()


# --- 4 and 5. every share gets a line in Slack ---------------------------------------


@pytest.fixture
def client(monkeypatch, slack):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA, "smoke": SMOKE})
    monkeypatch.setattr(bs_main, "_process_download", _noop)
    monkeypatch.setattr(bs_main, "_notify_slack", _noop)
    monkeypatch.setattr(bs_main, "_detail_best_effort", _noop)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    monkeypatch.setattr(bs_main, "annas_scraper", AnnasArchiveScraper())
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return TestClient(bs_main.app)


def test_a_share_that_breaks_the_endpoint_still_reaches_slack(client, slack, monkeypatch):
    """Today's shortcut never reads the reply, so the reply alone is silence."""
    def broken(shared):
        raise RuntimeError("boom")

    monkeypatch.setattr(bs_main, "extract_md5", broken)

    client.post("/api/download-url", headers={"X-Api-Key": "test-key", "X-Book-Url": MD5})

    assert len(slack) == 1 and slack[0].startswith("⚠️")


def test_a_second_recipient_joining_a_running_job_is_told_in_slack(client, slack):
    share = {"X-Api-Key": "test-key", "X-Book-Url": MD5, "X-Book-Title": "Dune"}
    client.post("/api/download-url", headers={**share, "X-Deliver-To": "anca"})

    client.post("/api/download-url", headers={**share, "X-Deliver-To": "smoke"})

    assert len(slack) == 1
    assert "Smoke's Kindle" in slack[0] and "again" in slack[0]


# --- 7 and 9. the job carries the book that was sent, under its real name ------------


async def test_a_corrected_id_is_the_one_the_job_records(tmp_path, monkeypatch, slack):
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path, [
        (263, "CISSP Official Study Guide", "Mike Chapple & Darril Gibson", {"PDF": 1}),
        (498, "Neuromancer", "William Gibson", {"EPUB": 1}),
    ])))
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)

    async def send(book_id, title, email):
        return None

    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    job = {"status": "done", "title": "Neuromancer", "author": "William Gibson", "md5": MD5,
           "book_id": 263, "kindle_email": ANCA, "message": "Added to Calibre"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._settle_job("j")

    assert job["book_id"] == 498


async def test_the_email_carries_the_title_the_library_gave_a_bare_md5(tmp_path, monkeypatch, slack):
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path, [
        (498, "Neuromancer", "William Gibson", {"EPUB": 1}),
    ])))
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "ingest"))
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    subjects = []

    async def send(book_id, title, email):
        subjects.append(title)
        return None

    class Libgen:
        async def download_file(self, md5):
            return _epub("Neuromancer", "William Gibson"), "neuromancer.epub"

    async def upload(data, filename):
        return 498

    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", upload)
    monkeypatch.setattr(bs_main, "_calibre_max_id", lambda: 400)
    job = {"status": "queued", "title": "Unknown", "author": "Unknown Author", "md5": MD5,
           "message": "", "kindle_email": ANCA}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", MD5, "Unknown", "Unknown Author", None)

    assert subjects == ["Neuromancer"]
