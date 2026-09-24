"""Keep book-search's on-disk state out of the real /stacks-config.

Jobs, rescues and the Kindle send guard persist small files so that a restart cannot
lose them. Every test gets its own directory, an empty guard, no rescues and no waiters.
"""

import pytest

import backend.main as bs_main


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    path = tmp_path / "book-search-state"
    monkeypatch.setattr(bs_main, "STATE_DIR", str(path))
    monkeypatch.setattr(bs_main, "_kindle_sends", {})
    monkeypatch.setattr(bs_main, "_kindle_sending", set())
    monkeypatch.setattr(bs_main, "_job_events", {})
    monkeypatch.setattr(bs_main, "_rescues", {})
    return path
