"""The ingest endpoint's Claude checks: the record before download, the file after.

Nothing Claude refuses may reach Calibre, and nothing may reach Calibre unchecked
when a check was asked for, even if Claude cannot be reached.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.goodreads.verify import Verdict, VerifierUnavailable
from tests.test_goodreads_verify import make_epub

DETAILS = {
    "isbn": "0785217240", "published": "2019",
    "description": "The history books say I died.",
    "candidate": {"title": "Romanov", "author": "Nadine Brandes", "publisher": "Thomas Nelson",
                  "year": "2019", "language": "English", "ext": "epub",
                  "size_bytes": 2_400_000},
}


class FakeVerifier:
    configured = True

    def __init__(self, record=None, file=None):
        self.record = record or Verdict(True, "same novel")
        self.file = file or Verdict(True, "same novel")
        self.calls = []

    async def check_record(self, wanted, offered):
        self.calls.append(("record", wanted, offered))
        if isinstance(self.record, Exception):
            raise self.record
        return self.record

    async def check_file(self, wanted, offered, evidence):
        self.calls.append(("file", wanted, offered, evidence))
        if isinstance(self.file, Exception):
            raise self.file
        return self.file


@pytest.fixture
def endpoint(monkeypatch):
    state = {"downloads": 0, "uploads": 0}

    class Libgen:
        async def download_file(self, md5):
            state["downloads"] += 1
            return make_epub("Romanov", "Nadine Brandes", "Ekaterinburg, 1918. " * 400), "r.epub"

    async def uploaded(data, filename):
        state["uploads"] += 1
        return 700

    async def not_a_duplicate(title, author):
        return None

    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", uploaded)
    monkeypatch.setattr(bs_main, "_check_cwa_duplicate", not_a_duplicate)
    monkeypatch.setattr(bs_main, "_calibre_id_for", lambda title, author: 700)
    monkeypatch.setattr(bs_main, "_calibre_formats_for", lambda book_id: {})
    monkeypatch.setattr(bs_main, "GOODREADS_SHELF_ID", 0)
    monkeypatch.setattr(bs_main, "GOODREADS_KINDLE_EMAIL", "")

    def post(verifier, details=DETAILS):
        monkeypatch.setattr(bs_main, "goodreads_verifier", verifier)
        body = {"md5": "e" * 32, "title": "Romanov", "author": "Nadine Brandes"}
        if details is not None:
            body["verify"] = details
        return TestClient(bs_main.app).post(
            "/api/goodreads/ingest", headers={"X-Api-Key": "test-key"}, json=body,
        )

    return post, state


def test_a_record_claude_refuses_is_never_downloaded(endpoint):
    post, state = endpoint

    r = post(FakeVerifier(record=Verdict(False, "a study guide about the novel")))

    assert r.status_code == 200
    assert r.json() == {"status": "rejected", "stage": "record",
                        "reason": "a study guide about the novel"}
    assert state["downloads"] == 0


def test_a_file_claude_refuses_is_downloaded_but_never_imported(endpoint):
    post, state = endpoint
    verifier = FakeVerifier(file=Verdict(False, "the file is a different book"))

    r = post(verifier)

    assert r.json()["status"] == "rejected"
    assert r.json()["stage"] == "file"
    assert state["downloads"] == 1
    assert state["uploads"] == 0
    evidence = verifier.calls[1][3]
    assert evidence.title == "Romanov"
    assert "Ekaterinburg" in evidence.text


def test_both_checks_passing_imports_the_book(endpoint):
    post, state = endpoint
    verifier = FakeVerifier()

    r = post(verifier)

    assert r.json()["status"] == "ok"
    assert [c[0] for c in verifier.calls] == ["record", "file"]
    wanted, offered = verifier.calls[0][1], verifier.calls[0][2]
    assert wanted.isbn == "0785217240" and wanted.description.startswith("The history")
    assert offered.publisher == "Thomas Nelson"
    assert state["uploads"] == 1


@pytest.mark.parametrize("stage", ["record", "file"])
def test_claude_unreachable_is_a_503_and_nothing_is_imported(endpoint, stage):
    post, state = endpoint
    outage = VerifierUnavailable("queue full")
    verifier = FakeVerifier(**{stage: outage})

    r = post(verifier)

    assert r.status_code == 503
    assert "queue full" in r.json()["detail"]
    assert state["uploads"] == 0


def test_an_unconfigured_verifier_is_a_503_when_a_check_was_asked_for(endpoint):
    post, state = endpoint
    verifier = FakeVerifier()
    verifier.configured = False

    r = post(verifier)

    assert r.status_code == 503
    assert state["downloads"] == 0


def test_a_file_it_cannot_read_relies_on_the_record_check(endpoint, monkeypatch):
    post, state = endpoint

    class MobiLibgen:
        async def download_file(self, md5):
            state["downloads"] += 1
            return b"BOOKMOBI" + b"\x00" * 60_000, "r.mobi"

    monkeypatch.setattr(bs_main, "libgen_scraper", MobiLibgen())
    verifier = FakeVerifier()

    r = post(verifier, details={**DETAILS, "candidate": {**DETAILS["candidate"], "ext": "mobi"}})

    assert r.json()["status"] == "ok"
    assert [c[0] for c in verifier.calls] == ["record"]


def test_a_shelf_timeout_does_not_stop_the_kindle_send(endpoint, monkeypatch):
    """Calibre-Web took over 30 s to answer while it processed Release Me on
    2026-09-27. The timeout escaped, the endpoint answered 500 after the import,
    and the book never went to the Kindle; a retry would then find it already
    in Calibre and skip the send for good."""
    import httpx

    post, state = endpoint
    sent = []

    async def shelf_times_out(client, shelf_id, book_id):
        raise httpx.ReadTimeout("calibre-web is busy")

    async def logged_in(client):
        return True

    async def send(book_id, title, address, formats=None):
        sent.append((book_id, address, formats))
        return None

    monkeypatch.setattr(bs_main, "GOODREADS_SHELF_ID", 6)
    monkeypatch.setattr(bs_main, "_cwa_login", logged_in)
    monkeypatch.setattr(bs_main, "_add_to_shelf", shelf_times_out)
    monkeypatch.setattr(bs_main, "GOODREADS_KINDLE_EMAIL", "anca@kindle.com")
    monkeypatch.setattr(bs_main, "_calibre_formats_for", lambda book_id: {"EPUB": 900_000})
    monkeypatch.setattr(bs_main, "_send_to_kindle", send)

    r = post(FakeVerifier())

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "ReadTimeout" in body["shelf_error"]
    assert body["kindle_sent"] is True
    assert sent == [(700, "anca@kindle.com", ("epub",))]


def test_without_a_verify_block_the_endpoint_behaves_as_before(endpoint):
    """Hand-run ingests and old pollers send no verify block and get no check."""
    post, state = endpoint
    verifier = FakeVerifier(record=Verdict(False, "would refuse"))

    r = post(verifier, details=None)

    assert r.json()["status"] == "ok"
    assert verifier.calls == []
