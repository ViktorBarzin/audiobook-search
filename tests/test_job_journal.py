"""A share that a restart kills still gets an answer.

book-search keeps its jobs in memory and restarts on every deploy: 9 rollouts on
2026-09-07 and 2 more on 2026-09-24. PDF shares that morning ran 94 s to 5 min,
long enough for a rollout to land in the middle. Each job now leaves a small
file under STATE_DIR/jobs while it runs. A pod shutting down reports its own
unfinished jobs as lost; a pod that died without that chance leaves files whose
heartbeat stops, and the next pod reports them.
"""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from backend.annas import AnnasArchiveScraper

MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"


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
def client(monkeypatch, slack):
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    monkeypatch.setattr(bs_main, "_process_download", _noop)
    monkeypatch.setattr(bs_main, "_notify_slack", _noop)
    monkeypatch.setattr(bs_main, "_detail_best_effort", _noop)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    monkeypatch.setattr(bs_main, "annas_scraper", AnnasArchiveScraper())
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    return TestClient(bs_main.app)


def entry(state_dir, job_id):
    return state_dir / "jobs" / f"{job_id}.json"


def leftover(state_dir, job_id, age_seconds, title="Remember Me?"):
    """A journal file left by another pod, last touched `age_seconds` ago."""
    path = entry(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": job_id, "title": title,
                                "author": "Sophie Kinsella", "md5": MD5,
                                "kindle_email": None, "pod": "book-search-old"}))
    then = time.time() - age_seconds
    os.utime(path, (then, then))
    return path


def test_a_new_share_leaves_a_journal_entry(client, state_dir):
    r = client.post("/api/download-url",
                    headers={"X-Api-Key": "test-key", "X-Book-Url": MD5,
                             "X-Book-Title": "Obviously Awesome"})

    saved = json.loads(entry(state_dir, r.json()["job_id"]).read_text())
    assert saved["md5"] == MD5
    assert saved["title"] == "Obviously Awesome"


async def test_a_finished_job_removes_its_entry(monkeypatch, slack, state_dir):
    class Nothing:
        async def download_file(self, md5):
            return None, None

    async def no_title_match(title, author, skip=frozenset(), want=None):
        return None, None, None

    monkeypatch.setattr(bs_main, "libgen_scraper", Nothing())
    monkeypatch.setattr(bs_main, "_libgen_find_file", no_title_match)
    monkeypatch.setattr(bs_main, "annas_scraper", None)
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    job = {"status": "queued", "title": "Remember Me?", "author": "Sophie Kinsella",
           "md5": MD5, "message": "", "kindle_email": None}
    monkeypatch.setattr(bs_main, "_download_jobs", {"j": job})
    bs_main._journal_write("j", job)
    assert entry(state_dir, "j").exists()

    await bs_main._process_download("j", MD5, "Remember Me?", "Sophie Kinsella", None)

    assert not entry(state_dir, "j").exists()


async def test_a_job_orphaned_by_a_dead_pod_is_reported_once(monkeypatch, slack, state_dir):
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    monkeypatch.setattr(bs_main, "_ttl_cleanup_job", _noop)
    path = leftover(state_dir, "dead1", age_seconds=600)

    await bs_main._recover_lost_jobs()
    await bs_main._recover_lost_jobs()

    assert len(slack) == 1
    assert "*Remember Me?*" in slack[0]
    assert "restart" in slack[0]
    assert not path.exists()
    tomb = bs_main._download_jobs["dead1"]
    assert tomb["finished"] is True
    assert "share it again" in tomb["final_text"].lower()


async def test_a_job_another_pod_is_still_running_is_left_alone(monkeypatch, slack, state_dir):
    """During a rolling update the old pod keeps its jobs fresh until it stops."""
    monkeypatch.setattr(bs_main, "_download_jobs", {})
    path = leftover(state_dir, "alive1", age_seconds=5)

    await bs_main._recover_lost_jobs()

    assert slack == []
    assert path.exists()


async def test_shutting_down_reports_the_jobs_still_running(monkeypatch, slack, state_dir):
    running = {"status": "downloading", "title": "Moby-Dick", "author": "Herman Melville",
               "md5": "b" * 32, "kindle_email": None, "finished": False}
    done = {"status": "done", "title": "Dune", "author": "Frank Herbert",
            "md5": "c" * 32, "kindle_email": None, "finished": True}
    monkeypatch.setattr(bs_main, "_download_jobs", {"r": running, "d": done})
    bs_main._journal_write("r", running)

    await bs_main._report_jobs_lost_on_shutdown()

    assert len(slack) == 1
    assert "*Moby-Dick*" in slack[0]
    assert not entry(state_dir, "r").exists()


def test_the_heartbeat_keeps_running_jobs_fresh(monkeypatch, state_dir):
    job = {"status": "downloading", "title": "Moby-Dick", "md5": "b" * 32, "finished": False}
    monkeypatch.setattr(bs_main, "_download_jobs", {"r": job})
    bs_main._journal_write("r", job)
    stale = time.time() - 300
    os.utime(entry(state_dir, "r"), (stale, stale))

    bs_main._journal_heartbeat()

    assert entry(state_dir, "r").stat().st_mtime > stale + 200


def test_an_unwritable_state_directory_does_not_break_a_share(client, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setattr(bs_main, "STATE_DIR", str(blocker / "state"))

    r = client.post("/api/download-url", headers={"X-Api-Key": "test-key", "X-Book-Url": MD5})

    assert r.status_code == 200
    assert r.json()["job_id"]
