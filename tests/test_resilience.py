"""Tests for SMTP retry + leftover ingest cleanup.

The user's pipeline failed twice: once when an orphan was emailed instead of
the requested book (covered by test_ingest.py), and once when SMTP transiently
disconnected on the first attempt and surfaced as a 500 to the iOS Shortcut.
These tests pin the retry behaviour and the post-job cleanup that prevents
partial files from leaking into subsequent jobs.
"""

import os
import smtplib
from unittest.mock import MagicMock

import pytest

from backend import main as bs_main


def _write(path, content=b"x" * 100_000):
    with open(path, "wb") as f:
        f.write(content)


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path))
    return tmp_path


# ---- SMTP retry ----


def _stub_smtp_class(side_effect_per_attempt):
    """Build a mock SMTP class whose context manager fails per-call sequence."""
    calls = {"n": 0}

    def factory(*args, **kwargs):
        calls["n"] += 1
        idx = calls["n"] - 1
        effect = side_effect_per_attempt[idx]
        ctx = MagicMock()
        if isinstance(effect, Exception):
            ctx.__enter__.side_effect = effect
        else:
            ctx.__enter__.return_value = MagicMock()
        ctx.__exit__.return_value = False
        return ctx

    factory.calls = calls
    return factory


def test_smtp_retry_succeeds_first_attempt(monkeypatch):
    factory = _stub_smtp_class([None])
    monkeypatch.setattr(smtplib, "SMTP", factory)
    monkeypatch.setattr(bs_main.time, "sleep", lambda *_: None)
    bs_main._smtp_send_with_retry(MagicMock(), max_attempts=3)
    assert factory.calls["n"] == 1


def test_smtp_retry_succeeds_after_transient_failures(monkeypatch):
    factory = _stub_smtp_class([
        smtplib.SMTPServerDisconnected("connection closed"),
        TimeoutError("banner timeout"),
        None,  # success
    ])
    monkeypatch.setattr(smtplib, "SMTP", factory)
    monkeypatch.setattr(bs_main.time, "sleep", lambda *_: None)
    bs_main._smtp_send_with_retry(MagicMock(), max_attempts=3)
    assert factory.calls["n"] == 3


def test_smtp_retry_exhausts_and_raises_last_exception(monkeypatch):
    factory = _stub_smtp_class([
        smtplib.SMTPServerDisconnected("attempt 1"),
        smtplib.SMTPServerDisconnected("attempt 2"),
        smtplib.SMTPServerDisconnected("attempt 3"),
    ])
    monkeypatch.setattr(smtplib, "SMTP", factory)
    monkeypatch.setattr(bs_main.time, "sleep", lambda *_: None)
    with pytest.raises(smtplib.SMTPServerDisconnected) as exc_info:
        bs_main._smtp_send_with_retry(MagicMock(), max_attempts=3)
    assert "attempt 3" in str(exc_info.value)
    assert factory.calls["n"] == 3


def test_smtp_retry_does_not_retry_on_non_retriable(monkeypatch):
    factory = _stub_smtp_class([smtplib.SMTPAuthenticationError(535, b"bad creds")])
    monkeypatch.setattr(smtplib, "SMTP", factory)
    monkeypatch.setattr(bs_main.time, "sleep", lambda *_: None)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        bs_main._smtp_send_with_retry(MagicMock(), max_attempts=3)
    assert factory.calls["n"] == 1


# ---- Cleanup of leftover ingest files ----


def test_cleanup_removes_new_arrivals_outside_snapshot(ingest):
    pre = {"orphan.epub"}
    _write(ingest / "orphan.epub")
    _write(ingest / "leftover.epub")
    removed = bs_main._cleanup_unconsumed_ingest_files("job123", pre)
    assert removed == ["leftover.epub"]
    assert sorted(os.listdir(ingest)) == ["orphan.epub"]


def test_cleanup_leaves_pre_existing_alone(ingest):
    _write(ingest / "older.epub")
    pre = {"older.epub"}
    removed = bs_main._cleanup_unconsumed_ingest_files("job123", pre)
    assert removed == []
    assert sorted(os.listdir(ingest)) == ["older.epub"]


def test_cleanup_ignores_non_ebook_files(ingest):
    _write(ingest / "stray.txt", b"meta")
    removed = bs_main._cleanup_unconsumed_ingest_files("job123", set())
    assert removed == []
    assert sorted(os.listdir(ingest)) == ["stray.txt"]


def test_cleanup_handles_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "missing"))
    assert bs_main._cleanup_unconsumed_ingest_files("job", set()) == []


def test_cleanup_removes_partial_mismatch_left_during_job(ingest):
    pre = set()
    _write(ingest / "real.epub")
    _write(ingest / "leftover.MISMATCH.epub", b"too small")
    removed = sorted(bs_main._cleanup_unconsumed_ingest_files("job", pre))
    assert removed == ["leftover.MISMATCH.epub", "real.epub"]
    assert os.listdir(ingest) == []
