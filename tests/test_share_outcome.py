"""Every share ends with one answer, and it names the right book.

Until 2026-09-24 a share went quiet once it started: the shortcut showed no
reply and Slack only heard about the start. That morning two books for Anca's
Kindle never arrived and nobody knew until Viktor asked. The end of
_process_download is now the one place a share is settled. The Kindle step runs
inside its own guard, the outcome is worked out once, the job is marked finished
so a waiting phone hears straight away, and Slack gets exactly one line,
success included.

The id the Kindle step sends comes from the library. _upload_to_calibre gets its
id from an OPDS search whose first term is the author, so for a libgen file name
it can name another book by the same author.
"""

import asyncio
import io
import zipfile

import pytest

import backend.main as bs_main
from tests.fake_library import make_library

ANCA = "ancaelena98_4RMJsy@kindle.com"
MD5 = "d7191a16e9bc05c7afcf0a0c53600089"
EPUB = b"PK\x03\x04" + b"x" * 50_000


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
def pipeline(monkeypatch, tmp_path, slack):
    """libgen hands over an EPUB, Calibre-Web accepts it, the library holds it."""
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "ingest"))
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path, [
        (511, "Remember Me?", "Sophie Kinsella", {"EPUB": 400_000}),
    ])))
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    monkeypatch.setattr(bs_main, "CALIBRE_ID_ATTEMPTS", 2)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)

    class Libgen:
        async def download_file(self, md5):
            return EPUB, "Sophie Kinsella - Remember Me_ - libgen.li.epub"

    async def uploaded(data, filename):
        return 511

    sends = []

    async def send(book_id, title, email):
        sends.append((book_id, email))
        return None

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", uploaded)
    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    return sends


def register(monkeypatch, kindle_email=ANCA, title="Remember Me?", author="Sophie Kinsella"):
    job = {"status": "queued", "title": title, "author": author, "md5": MD5,
           "message": "", "kindle_email": kindle_email}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})
    return job


async def test_a_delivered_book_ends_with_one_success_line(pipeline, slack, monkeypatch):
    job = register(monkeypatch)

    await bs_main._process_download("j", MD5, "Remember Me?", "Sophie Kinsella", None)

    assert pipeline == [(511, ANCA)]
    assert job["finished"] is True
    assert job["outcome"] == "done"
    assert job["final_text"].startswith("✅")
    assert "Remember Me?" in job["final_text"]
    assert "Anca's Kindle" in job["final_text"]
    assert len(slack) == 1, slack
    assert slack[0].startswith("✅")
    assert "*Remember Me?*" in slack[0]
    assert "epub" in slack[0]


async def test_a_calibre_only_share_says_it_reached_calibre(pipeline, slack, monkeypatch):
    job = register(monkeypatch, kindle_email=None)

    await bs_main._process_download("j", MD5, "Remember Me?", "Sophie Kinsella", None)

    assert pipeline == []
    assert job["outcome"] == "done"
    assert "Calibre" in job["final_text"]
    assert "Kindle" not in job["final_text"]
    assert len(slack) == 1 and slack[0].startswith("✅")


async def test_a_failed_share_says_why_on_the_phone_and_in_slack(pipeline, slack, monkeypatch):
    class Nothing:
        async def download_file(self, md5):
            return None, None

    async def no_title_match(title, author, skip=frozenset(), want=None):
        return None, None, None

    monkeypatch.setattr(bs_main, "libgen_scraper", Nothing())
    monkeypatch.setattr(bs_main, "_libgen_find_file", no_title_match)
    monkeypatch.setattr(bs_main, "annas_scraper", None)
    job = register(monkeypatch)

    await bs_main._process_download("j", MD5, "Remember Me?", "Sophie Kinsella", None)

    assert job["finished"] is True
    assert job["outcome"] == "failed"
    assert job["final_text"].startswith("⚠️")
    assert "Anca's Kindle" in job["final_text"]
    assert len(slack) == 1 and slack[0].startswith("⚠️")
    assert "*Remember Me?*" in slack[0]


async def test_a_kindle_step_that_raises_still_finishes_the_job(pipeline, slack, monkeypatch):
    """Before this, an exception there skipped the Slack line and the TTL."""
    async def explodes(book_id, title, email):
        raise RuntimeError("smtp library bug")

    ttl = []

    async def record_ttl(job_id):
        ttl.append(job_id)

    monkeypatch.setattr(bs_main, "_send_to_kindle", explodes)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", record_ttl)
    job = register(monkeypatch)

    await bs_main._process_download("j", MD5, "Remember Me?", "Sophie Kinsella", None)
    await asyncio.sleep(0)

    assert job["finished"] is True
    assert job["outcome"] == "failed"
    assert len(slack) == 1 and slack[0].startswith("⚠️")
    assert ttl == ["j"]


async def test_the_job_is_finished_before_slack_is_told(pipeline, monkeypatch):
    """A waiting phone must not sit behind a slow Slack call."""
    seen = {}

    async def slow_slack(text):
        seen["finished_when_posting"] = bs_main._download_jobs["j"].get("finished")

    monkeypatch.setattr(bs_main, "_post_slack", slow_slack)
    register(monkeypatch)

    await bs_main._process_download("j", MD5, "Remember Me?", "Sophie Kinsella", None)

    assert seen["finished_when_posting"] is True


async def test_an_opds_id_for_another_book_by_the_author_is_not_sent(pipeline, monkeypatch, tmp_path):
    """The OPDS search leads with the author, so it can land on the wrong Gibson."""
    lib = make_library(tmp_path / "gibsons", [
        (263, "CISSP Official Study Guide", "Mike Chapple & Darril Gibson", {"PDF": 9_000_000}),
        (498, "Neuromancer", "William Gibson", {"EPUB": 300_000}),
    ])
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(lib))

    async def opds_guess(data, filename):
        return 263

    monkeypatch.setattr(bs_main, "_upload_to_calibre", opds_guess)
    job = register(monkeypatch, title="Neuromancer", author="William Gibson")

    await bs_main._process_download("j", MD5, "Neuromancer", "William Gibson", None)

    assert pipeline == [(498, ANCA)]
    assert job["book_id"] == 498


async def test_the_kindle_step_confirms_an_id_no_upload_step_checked(pipeline, monkeypatch, tmp_path):
    """The Stacks routes still take their id straight from an OPDS search."""
    lib = make_library(tmp_path / "stacks", [
        (263, "CISSP Official Study Guide", "Mike Chapple & Darril Gibson", {"PDF": 9_000_000}),
        (498, "Neuromancer", "William Gibson", {"EPUB": 300_000}),
    ])
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(lib))
    job = {"status": "done", "title": "Neuromancer", "author": "William Gibson",
           "md5": MD5, "book_id": 263, "kindle_email": ANCA, "message": "Added to Calibre"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._settle_job("j")

    assert pipeline == [(498, ANCA)]


async def test_an_upload_the_library_never_shows_is_not_reported_as_added(pipeline, slack, monkeypatch, tmp_path):
    lib = make_library(tmp_path / "other", [
        (263, "CISSP Official Study Guide", "Mike Chapple & Darril Gibson", {"PDF": 9_000_000}),
    ])
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(lib))

    async def opds_guess(data, filename):
        return 263

    monkeypatch.setattr(bs_main, "_upload_to_calibre", opds_guess)
    job = register(monkeypatch, title="Neuromancer", author="William Gibson")

    await bs_main._process_download("j", MD5, "Neuromancer", "William Gibson", None)

    assert pipeline == [], "nothing is emailed when the book cannot be found"
    assert job["outcome"] == "failed"
    assert "library" in job["final_text"]
    assert slack[0].startswith("⚠️")


def _epub_with(title, author):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip")
        epub.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ))
        epub.writestr("OEBPS/content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
            "</metadata></package>"
        ))
    return buffer.getvalue() + b"\0" * 6000


def test_an_epub_names_its_own_title_and_author():
    assert bs_main._ebook_meta(_epub_with("The Hobbit", "J. R. R. Tolkien"), "x.epub") == (
        "The Hobbit", "J. R. R. Tolkien")


def test_a_file_that_is_not_an_epub_names_nothing():
    assert bs_main._ebook_meta(b"%PDF-1.4" + b"x" * 6000, "x.pdf") is None
    assert bs_main._ebook_meta(b"PK\x03\x04garbage", "x.epub") is None


async def test_the_files_own_title_finds_the_book_when_the_page_title_differs(pipeline, monkeypatch, tmp_path):
    """Calibre files an EPUB under its OPF title, not the Anna's Archive one."""
    lib = make_library(tmp_path / "hobbit", [
        (520, "The Hobbit", "J. R. R. Tolkien", {"EPUB": 500_000}),
    ])
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(lib))
    book = _epub_with("The Hobbit", "J. R. R. Tolkien")

    class Libgen:
        async def download_file(self, md5):
            return book, "Tolkien - The Hobbit - libgen.li.epub"

    async def not_found_yet(data, filename):
        return -1

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", not_found_yet)
    title = "The Hobbit, or There and Back Again"
    register(monkeypatch, title=title, author="J.R.R. Tolkien")

    await bs_main._process_download("j", MD5, title, "J.R.R. Tolkien", None)

    assert pipeline == [(520, ANCA)]


@pytest.mark.parametrize("line, joined", [
    ("⚠️ Sleepy Hollow did not reach Calibre: All download methods failed",
     "⚠️ Sleepy Hollow did not reach Calibre: All download methods failed. Trying again in 15 minutes."),
    ("⚠️ It failed.", "⚠️ It failed. Trying again in 15 minutes."),
    ("✅ Moby-Dick → Calibre (epub, 41 s)", "✅ Moby-Dick → Calibre (epub, 41 s). Trying again in 15 minutes."),
])
def test_a_note_after_a_line_starts_a_new_sentence(line, joined):
    """Live 2026-09-24: "...All download methods failed Trying again in 15 minutes."."""
    assert bs_main._and_then(line, "Trying again in 15 minutes.") == joined
    assert bs_main._and_then(line, "") == line


@pytest.mark.parametrize("kindle", [ANCA, None])
async def test_a_book_stacks_already_fetched_is_found_in_the_library(monkeypatch, tmp_path, kindle):
    """Stacks remembers the md5 and the OPDS check misses the book, but it is in
    the library. It must be found there, not re-downloaded or failed as having
    no route (which would also open a paid rescue)."""
    posted = []

    async def record(text):
        posted.append(text)

    monkeypatch.setattr(bs_main, "_post_slack", record)
    monkeypatch.setattr(bs_main, "KINDLE_RECIPIENTS", {"anca": ANCA})
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(ingest))
    monkeypatch.setattr(bs_main, "CWA_LIBRARY_PATH", str(make_library(tmp_path / "lib", [
        (507, "Remember Me?", "Sophie Kinsella", {"EPUB": 400_000}),
    ])))
    monkeypatch.setattr(bs_main, "CALIBRE_ID_INTERVAL", 0)
    monkeypatch.setattr(bs_main, "CALIBRE_ID_ATTEMPTS", 2)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    monkeypatch.setattr(bs_main, "API_KEY", "k")
    monkeypatch.setattr(bs_main, "CLAUDE_AGENT_URL", "http://agent.test")
    monkeypatch.setattr(bs_main, "CLAUDE_AGENT_TOKEN", "t")
    monkeypatch.setattr(bs_main, "_sources_down", _noop)
    redownloads = []

    async def no_direct(*a, **k):
        return False

    monkeypatch.setattr(bs_main, "_try_direct_download", no_direct)

    class Annas:
        async def download_via_stacks(self, md5):
            return {"success": True, "message": "Already downloaded"}

        async def stacks_force_redownload(self, md5):
            redownloads.append(md5)
            return {"success": False, "error": "stacks said no"}

    monkeypatch.setattr(bs_main, "annas_scraper", Annas())

    async def opds_miss(title, timeout=120):
        return None

    monkeypatch.setattr(bs_main, "_wait_for_calibre", opds_miss)
    sends = []

    async def send(book_id, title, email):
        sends.append((book_id, email))

    monkeypatch.setattr(bs_main, "_send_to_kindle", send)
    md5 = "d7191a16e9bc05c7afcf0a0c53600089"
    job = {"status": "queued", "title": "Remember Me?", "author": "Sophie Kinsella", "md5": md5,
           "message": "", "kindle_email": kindle, "created_at": 0}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", md5, "Remember Me?", "Sophie Kinsella", None)

    assert job["outcome"] == "done"
    assert sends == ([(507, ANCA)] if kindle else [])
    assert redownloads == [], "no second download of a book the library has"
    assert "j" not in bs_main._rescues
