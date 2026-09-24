"""A share that does not reach the Kindle has to say so where Viktor looks.

Live 2026-09-24: two books were shared for Anca's Kindle and neither arrived.
The first share posted a 1.9 KB page with no book in it and got a 400 that
nobody saw, because the shortcut does not show the response. The second posted
"→ Kindle" to Slack when the job started, then failed later without a word.
Viktor only found out by asking. The start line stays, since it is what caught
a wrong pick (the Backman collection) that same morning; what is new is one
warning line whenever a share ends without doing what it asked.
"""

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper


async def _noop(*a, **k):
    return None


@pytest.fixture
def slack(monkeypatch):
    posted = []

    def record(text):
        posted.append(text)
        return _noop()

    monkeypatch.setattr(bs_main, "_post_slack", record)
    return posted


@pytest.fixture
def client(monkeypatch, slack):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    monkeypatch.setattr(bs_main, "_process_download", _noop)
    monkeypatch.setattr(bs_main, "_notify_slack", _noop)
    monkeypatch.setattr(bs_main, "_detail_best_effort", _noop)
    monkeypatch.setattr(bs_main, "annas_scraper", AnnasArchiveScraper())
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return TestClient(bs_main.app)


def test_a_share_with_no_book_in_it_says_so(client, slack):
    """The 05:48 request: 1,897 bytes of HTML, no link, no md5."""
    head, tail = "<!DOCTYPE html><html><head><title>Checking</title></head><body>", "</body></html>"
    page = head + "x" * (1897 - len(head) - len(tail)) + tail

    r = client.post("/api/download-url", content=page.encode(),
                    headers={"X-Api-Key": "test-key", "Content-Type": "text/html"})

    assert r.status_code == 400
    assert "finish loading" in r.json()["detail"]
    assert len(slack) == 1
    assert slack[0].startswith("⚠️")
    assert "1.9 KB" in slack[0]


def test_a_link_that_names_no_book_says_so(client, slack):
    r = client.post("/api/download-url", json={"url": "https://annas-archive.org/search?q=ove"},
                    headers={"X-Api-Key": "test-key"})

    assert r.status_code == 400
    assert len(slack) == 1
    assert slack[0].startswith("⚠️")
    assert "annas-archive.org/search?q=ove" in slack[0]


def test_a_problem_line_names_the_book_and_the_reason():
    line = bs_main._format_share_problem(
        "Remember Me", "Sophie Kinsella", "Calibre did not import it", kindle=True,
    )

    assert line.startswith("⚠️")
    assert "*Remember Me*" in line
    assert "Kindle" in line
    assert "Calibre did not import it" in line


@pytest.mark.parametrize("job, problem", [
    ({"status": "failed", "message": "All download methods failed"}, "All download methods failed"),
    ({"status": "done", "kindle_email": "r@kindle.com", "kindle_attempted": True,
      "message": "Added to Calibre and sent to r@kindle.com"}, None),
    ({"status": "done", "kindle_email": "r@kindle.com",
      "message": "Uploaded — Calibre import may still be processing"},
     "Uploaded — Calibre import may still be processing"),
    ({"status": "done", "kindle_email": None, "message": "Added to Calibre"}, None),
])
def test_which_finished_jobs_count_as_a_problem(job, problem):
    assert bs_main._delivery_problem(job) == problem


class FailingLibgen:
    async def download_file(self, md5):
        return None, None


async def test_a_job_that_fails_posts_one_warning(monkeypatch, slack):
    async def no_title_match(title, author, skip_md5=None):
        return None, None

    monkeypatch.setattr(bs_main, "libgen_scraper", FailingLibgen())
    monkeypatch.setattr(bs_main, "_libgen_by_title", no_title_match)
    monkeypatch.setattr(bs_main, "annas_scraper", None)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    md5 = "cab14d02fedc672f7e51682b78e477a4"
    job = {"status": "queued", "title": "A Man Called Ove: A Novel", "author": "Fredrik Backman",
           "md5": md5, "message": "", "kindle_email": "reader@kindle.com"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", md5, "A Man Called Ove: A Novel", "Fredrik Backman", None)

    assert job["status"] == "failed"
    assert len(slack) == 1
    assert "*A Man Called Ove: A Novel*" in slack[0]
    assert "Kindle" in slack[0]


async def test_a_delivered_book_posts_no_warning(monkeypatch, slack):
    class Libgen:
        async def download_file(self, md5):
            return b"PK\x03\x04" + b"x" * 50_000, "Remember Me.epub"

    async def uploaded(data, filename):
        return 511

    async def sent(book_id, title, email):
        return None

    monkeypatch.setattr(bs_main, "libgen_scraper", Libgen())
    monkeypatch.setattr(bs_main, "_upload_to_calibre", uploaded)
    monkeypatch.setattr(bs_main, "_send_to_kindle", sent)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    md5 = "d7191a16e9bc05c7afcf0a0c53600089"
    job = {"status": "queued", "title": "Remember Me?", "author": "Sophie Kinsella",
           "md5": md5, "message": "", "kindle_email": "reader@kindle.com"}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})

    await bs_main._process_download("j", md5, "Remember Me?", "Sophie Kinsella", None)

    assert "sent to" in job["message"]
    assert slack == []
