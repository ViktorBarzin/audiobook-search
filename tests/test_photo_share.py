"""A book from a photo of its front cover (docs/2026-10-29-book-from-a-photo.md).

The phone shortcut posts the photo to /api/download-url and the web page posts
it to /photo-share. Claude reads the cover; the book Calibre already holds is
sent as it is; otherwise the matcher looks on libgen in the cover's language,
the Claude check confirms the record and the file, and the share ends through
_settle_job like any other: one line on the phone and in Slack.
"""

import asyncio
import io
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.main as bs_main
from backend.goodreads.matcher import Candidate
from backend.goodreads.verify import FileEvidence, VerifierUnavailable, Verdict
from backend.photo import CoverReaderUnavailable, CoverReading
from tests.fake_library import make_library

ANCA = "anca@kindle.com"
EPUB = b"PK\x03\x04" + b"x" * 50_000
KEY = "test-key"


def jpeg_bytes(size=(1200, 1600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, "JPEG")
    return out.getvalue()


def row(md5, title="Под игото", author="Иван Вазов", language="Bulgarian", ext="epub"):
    return Candidate(md5=md5, title=title, author=author, ext=ext, language=language,
                     size_bytes=400_000, source="libgen")


async def _noop(*a, **k):
    return None


@pytest.fixture
def slack(monkeypatch):
    posted = []

    async def record(text):
        posted.append(text)

    monkeypatch.setattr(bs_main, "_post_slack", record)
    return posted


class Reader:
    def __init__(self, reading=None, error=None):
        self.reading, self.error, self.calls = reading, error, 0

    async def read(self, jpeg):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reading


class Gate:
    """The Claude check: refuses the formats it is told to, at either stage."""

    def __init__(self, refuse_record=(), refuse_file=(), down=False):
        self.refuse_record, self.refuse_file, self.down = set(refuse_record), set(refuse_file), down
        self.asked = []
        self.configured = True

    async def check_record(self, wanted, offered):
        self.asked.append(("record", offered.title, wanted.language))
        if self.down:
            raise VerifierUnavailable("agent service down")
        ok = offered.ext not in self.refuse_record
        return Verdict(ok, "same book" if ok else "a different book")

    async def check_file(self, wanted, offered, evidence):
        self.asked.append(("file", offered.title, wanted.language))
        ok = offered.ext not in self.refuse_file
        return Verdict(ok, "same book" if ok else "the file is a summary")


class Libgen:
    def __init__(self, rows):
        self.rows, self.queries, self.downloads = rows, [], []

    async def search_candidates(self, query):
        self.queries.append(query)
        return list(self.rows)

    async def download_file(self, md5):
        self.downloads.append(md5)
        return EPUB, f"{md5}.epub"

    async def _get_mirror(self):
        return "https://libgen.li"


def add_book(library, book_id, title, author):
    conn = sqlite3.connect(library / "metadata.db")
    conn.execute("INSERT INTO books VALUES (?,?,?)", (book_id, title, "2026-10-29 10:00:00+00:00"))
    conn.execute("INSERT INTO authors VALUES (?,?)", (book_id, author))
    conn.execute("INSERT INTO books_authors_link VALUES (?,?)", (book_id, book_id))
    conn.execute("INSERT INTO data VALUES (?,?,?)", (book_id, "EPUB", 400_000))
    conn.commit()
    conn.close()


@pytest.fixture
def world(monkeypatch, tmp_path, slack):
    """A library with one unrelated book; uploads land in it as book 900."""
    library = make_library(tmp_path / "library", [(1, "Dune", "Frank Herbert", {"EPUB": 1})])
    monkeypatch.setattr(bs_main, "API_KEY", KEY)
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(library))
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "ingest"))
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    monkeypatch.setattr(bs_main, "CALIBRE_ID_ATTEMPTS", 2)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    monkeypatch.setattr(bs_main, "CLAUDE_AGENT_URL", "")

    uploads = []

    async def uploaded(data, filename):
        uploads.append(filename)
        add_book(library, 900, "Под игото", "Иван Вазов")
        return 900

    sends = []

    async def send(book_id, title, email, formats=None):
        sends.append((book_id, email))
        return None

    monkeypatch.setattr(bs_main, "_upload_to_calibre", uploaded)
    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    monkeypatch.setattr(bs_main, "extract_evidence",
                        lambda data, ext: FileEvidence(format="epub", title="Под игото"))

    class World:
        pass

    w = World()
    w.library, w.uploads, w.sends, w.slack = library, uploads, sends, slack

    def install(reading=None, reader_error=None, rows=(), gate=None):
        w.reader = Reader(reading, reader_error)
        w.libgen = Libgen(list(rows))
        w.gate = gate or Gate()
        monkeypatch.setattr(bs_main, "cover_reader", w.reader)
        monkeypatch.setattr(bs_main, "libgen_scraper", w.libgen)
        monkeypatch.setattr(bs_main, "goodreads_verifier", w.gate)

    w.install = install
    return w


VAZOV = CoverReading(readable=True, title="Под игото", author="Иван Вазов", language="bulgarian")


def photo_job(kindle_email=ANCA, **fields):
    job_id = "p1"
    job = {"status": "queued", "phase": "reading", "title": "", "author": "", "md5": None,
           "message": "", "kindle_email": kindle_email, "created_at": time.time(),
           "finished": False, "kind": "photo", **fields}
    bs_main._download_jobs[job_id] = job
    return job_id, job


# --------------------------------------------------------------------------- #
# Taking a photo in                                                            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def started(world, monkeypatch):
    """The endpoints, with the background work recorded instead of run."""
    runs = []

    def record(job_id, jpeg=None):
        runs.append((job_id, jpeg))
        return _noop()

    monkeypatch.setattr(bs_main, "_process_photo", record)
    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen([]))
    return runs


def test_the_shortcut_posts_a_photo_and_gets_a_job(started, state_dir):
    client = TestClient(bs_main.app)

    r = client.post("/api/download-url", content=jpeg_bytes(),
                    headers={"Content-Type": "image/jpeg", "X-Api-Key": KEY, "X-Deliver-To": "anca"})

    assert r.status_code == 200, r.text
    body = r.json()
    job = bs_main._download_jobs[body["job_id"]]
    assert job["kind"] == "photo"
    assert job["kindle_email"] == ANCA
    assert body["message"].startswith("📷")
    assert "Anca's Kindle" in body["message"]
    assert os.path.isfile(state_dir / "photos" / f"{body['job_id']}.jpg")
    assert started and started[0][0] == body["job_id"]
    assert Image.open(io.BytesIO(started[0][1])).format == "JPEG"


def test_a_photo_needs_the_api_key(started):
    r = TestClient(bs_main.app).post("/api/download-url", content=jpeg_bytes(),
                                     headers={"Content-Type": "image/jpeg"})

    assert r.status_code == 401
    assert not started


@pytest.mark.parametrize("headers,kwargs", [
    ({}, {"files": {"photo": ("cover.jpg", b"", "image/jpeg")}}),
    ({"Content-Type": "application/octet-stream"}, {"content": None}),
])
def test_other_ways_a_photo_arrives(started, headers, kwargs):
    if "files" in kwargs:
        kwargs = {"files": {"photo": ("cover.jpg", jpeg_bytes(), "image/jpeg")}}
    else:
        kwargs = {"content": jpeg_bytes()}

    r = TestClient(bs_main.app).post("/api/download-url", headers={"X-Api-Key": KEY, **headers}, **kwargs)

    assert r.status_code == 200, r.text
    assert len(started) == 1


def test_a_file_that_is_not_a_picture_is_turned_away(started):
    r = TestClient(bs_main.app).post("/api/download-url", content=b"\xff\xd8\xff not really",
                                     headers={"Content-Type": "image/jpeg", "X-Api-Key": KEY})

    assert r.status_code == 400
    body = r.json()
    assert body["message"].startswith("⚠️")
    assert bs_main._download_jobs[body["job_id"]]["code"] == "not_a_photo"
    assert not started


def test_a_link_share_still_goes_the_link_way(started):
    r = TestClient(bs_main.app).post("/api/download-url", content=b"no book here",
                                     headers={"Content-Type": "text/plain", "X-Api-Key": KEY})

    assert r.status_code == 400
    assert not started


def test_the_web_page_uploads_without_a_key_and_names_a_recipient(started):
    r = TestClient(bs_main.app).post(
        "/photo-share", files={"photo": ("IMG_0001.HEIC", jpeg_bytes(), "image/heic")},
        data={"deliver_to": "anca"})

    assert r.status_code == 200, r.text
    assert bs_main._download_jobs[r.json()["job_id"]]["kindle_email"] == ANCA


def test_the_web_page_cannot_send_to_an_arbitrary_address(started):
    r = TestClient(bs_main.app).post(
        "/photo-share", files={"photo": ("cover.jpg", jpeg_bytes(), "image/jpeg")},
        data={"kindle_email": "stranger@kindle.com"})

    assert r.status_code == 200
    assert bs_main._download_jobs[r.json()["job_id"]]["kindle_email"] is None


def test_the_web_page_with_no_photo_says_so(started):
    r = TestClient(bs_main.app).post("/photo-share", data={"deliver_to": "anca"})

    assert r.status_code == 400
    assert "photo" in r.json()["message"]


def test_a_waiting_phone_hears_the_cover_is_being_read(world):
    _, job = photo_job()

    assert bs_main._progress_line(job) == "⏳ Reading the cover…"


# --------------------------------------------------------------------------- #
# From a cover to a book                                                       #
# --------------------------------------------------------------------------- #

async def test_a_bulgarian_cover_reaches_the_kindle(world):
    world.install(VAZOV, rows=[row("e" * 32, "Under the Yoke", "Ivan Vazov", "English"), row("b" * 32)])
    job_id, job = photo_job()

    await bs_main._process_photo(job_id, b"jpeg")

    assert world.libgen.downloads == ["b" * 32]
    assert world.sends == [(900, ANCA)]
    assert job["outcome"] == "done", job
    assert job["final_text"].startswith("✅ Под игото by Иван Вазов (Bulgarian) → Anca's Kindle")
    assert [stage for stage, _, _ in world.gate.asked] == ["record", "file"]
    assert all(language == "bulgarian" for _, _, language in world.gate.asked)
    assert len(world.slack) == 1 and world.slack[0].startswith("✅ *Под игото*")


async def test_an_english_cover_does_not_name_its_language(world):
    reading = CoverReading(readable=True, title="Dune", author="Frank Herbert", language="english")
    world.install(reading, rows=[row("d" * 32, "Dune", "Frank Herbert", "English")])
    job_id, job = photo_job(kindle_email=None)

    await bs_main._process_photo(job_id, b"jpeg")

    # Dune is already in the library, so nothing is downloaded at all.
    assert world.libgen.downloads == []
    assert job["final_text"] == "✅ Dune by Frank Herbert was already in Calibre"


async def test_a_book_already_in_calibre_goes_to_the_kindle_as_it_is(world):
    add_book(world.library, 77, "Под игото", "Иван Вазов")
    world.install(VAZOV, rows=[row("b" * 32)])
    job_id, job = photo_job()

    await bs_main._process_photo(job_id, b"jpeg")

    assert world.libgen.queries == []
    assert world.sends == [(77, ANCA)]
    assert job["outcome"] == "done"
    assert "already in Calibre" in job["final_text"]


async def test_a_photo_with_no_readable_cover_downloads_nothing(world):
    world.install(CoverReading(readable=False, note="a blurry photo of a shelf"))
    job_id, job = photo_job()

    await bs_main._process_photo(job_id, b"jpeg")

    assert world.libgen.queries == []
    assert job["outcome"] == "failed"
    assert job["code"] == "unreadable"
    assert job["final_text"].startswith("⚠️")
    assert "a blurry photo of a shelf" in job["final_text"]
    assert len(world.slack) == 1


async def test_claude_being_down_is_not_a_miss(world):
    world.install(reader_error=CoverReaderUnavailable("agent service answered 503"))
    job_id, job = photo_job()

    await bs_main._process_photo(job_id, b"jpeg")

    assert job["code"] == "claude_down"
    assert "again later" in job["final_text"]


async def test_a_record_claude_refuses_makes_way_for_the_next(world):
    first, second = row("1" * 32, ext="epub"), row("2" * 32, ext="pdf")
    world.install(VAZOV, rows=[first, second], gate=Gate(refuse_record={"epub"}))
    job_id, job = photo_job(kindle_email=None)

    await bs_main._process_photo(job_id, b"jpeg")

    assert world.libgen.downloads == ["2" * 32]
    assert job["outcome"] == "done"


async def test_a_file_claude_refuses_makes_way_for_the_next(world):
    first, second = row("1" * 32, ext="epub"), row("2" * 32, ext="fb2")
    world.install(VAZOV, rows=[first, second], gate=Gate(refuse_file={"epub"}))
    job_id, job = photo_job(kindle_email=None)

    await bs_main._process_photo(job_id, b"jpeg")

    assert world.libgen.downloads == ["1" * 32, "2" * 32]
    assert world.uploads == ["2" * 32 + ".epub"]
    assert job["outcome"] == "done"


async def test_every_match_refused_ends_with_claudes_reason(world):
    world.install(VAZOV, rows=[row("1" * 32)], gate=Gate(refuse_record={"epub"}))
    job_id, job = photo_job(kindle_email=None)

    await bs_main._process_photo(job_id, b"jpeg")

    assert job["code"] == "no_route"
    assert "a different book" in job["final_text"]
    assert world.libgen.downloads == []


async def test_the_claude_check_being_down_downloads_nothing(world):
    world.install(VAZOV, rows=[row("1" * 32)], gate=Gate(down=True))
    job_id, job = photo_job(kindle_email=None)

    await bs_main._process_photo(job_id, b"jpeg")

    assert job["code"] == "claude_down"
    assert world.libgen.downloads == []


async def test_only_other_languages_is_a_miss_that_names_the_language(world):
    world.install(VAZOV, rows=[row("r" * 32, language="Russian")])
    job_id, job = photo_job()

    await bs_main._process_photo(job_id, b"jpeg")

    assert job["code"] == "no_language_edition"
    assert "Bulgarian" in job["final_text"]
    assert world.libgen.downloads == []
    assert not bs_main._rescue_eligible(job)


async def test_no_match_on_libgen_opens_a_rescue_that_retries_from_the_reading(world, monkeypatch):
    monkeypatch.setattr(bs_main, "CLAUDE_AGENT_URL", "http://agent")
    monkeypatch.setattr(bs_main, "CLAUDE_AGENT_TOKEN", "t")

    async def nothing_down():
        return None

    monkeypatch.setattr(bs_main, "_sources_down", nothing_down)
    world.install(VAZOV, rows=[row("x" * 32, title="Something Else", author="Other Person")])
    job_id, job = photo_job()

    await bs_main._process_photo(job_id, b"jpeg")

    assert job["code"] == "no_route"
    rescue = bs_main._rescues[job_id]
    assert rescue["kind"] == "photo"
    assert rescue["language"] == "bulgarian"
    assert rescue["key"].startswith("photo:")
    assert "Trying again in" in job["final_text"]

    # The retry looks again from the reading; the cover is not read twice.
    world.libgen.rows = [row("b" * 32)]
    bs_main._start_rerun(job_id, rescue)
    child_id = rescue["child_job_id"]
    for _ in range(50):
        if bs_main._download_jobs[child_id].get("finished"):
            break
        await asyncio.sleep(0.01)
    child = bs_main._download_jobs[child_id]
    assert child["outcome"] == "done", child
    assert world.reader.calls == 1
    assert world.libgen.downloads == ["b" * 32]


def test_the_rescue_prompt_names_the_language_and_the_photo(world):
    prompt = bs_main._rescue_prompt("p1", {
        "code": "no_route", "md5": None, "tried": [], "kindle_email": ANCA,
        "title": "Под игото", "author": "Иван Вазов", "kind": "photo", "language": "bulgarian",
    })

    assert "Language wanted: Bulgarian" in prompt
    assert "shared as a photo of the cover" in prompt


def test_photos_older_than_a_week_are_deleted(world, state_dir):
    folder = state_dir / "photos"
    folder.mkdir(parents=True)
    old, new = folder / "old.jpg", folder / "new.jpg"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    week_ago = time.time() - 8 * 24 * 3600
    os.utime(old, (week_ago, week_ago))

    bs_main._sweep_old_photos()

    assert not old.exists()
    assert new.exists()


def test_the_web_page_offers_the_photo_upload_and_the_recipients(world):
    page = TestClient(bs_main.app).get("/").text

    assert 'id="photoInput"' in page
    assert 'accept="image/*"' in page
    assert '<option value="anca">Anca\'s Kindle</option>' in page
    assert "__PHOTO_RECIPIENTS__" not in page
