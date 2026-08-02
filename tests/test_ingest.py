"""Tests for ingest-folder scanning + orphan filtering.

Regression: a stale .MISMATCH.epub file (154 bytes, AA rate-limit error page) was
left in /cwa-book-ingest/ from a failed download. A later unrelated job picked it
up via _wait_for_file_in_ingest() and emailed it to a Kindle. These tests pin the
filtering rules so that orphans can never poison new jobs again.
"""

import asyncio
import os

import pytest

from backend import main as bs_main


def _write(path, content=b"x" * 100_000):
    with open(path, "wb") as f:
        f.write(content)


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path))
    return tmp_path


def test_ingest_files_excludes_mismatch_suffix(ingest):
    _write(ingest / "good.epub")
    _write(ingest / "How to Stand.MISMATCH.epub")
    assert bs_main._ingest_ebook_files() == ["good.epub"]


def test_ingest_files_excludes_files_below_minimum_size(ingest):
    _write(ingest / "good.epub")
    _write(ingest / "rate-limit-stub.epub", b"<html>too many downloads</html>")
    assert bs_main._ingest_ebook_files() == ["good.epub"]


def test_ingest_files_excludes_non_ebook_extensions(ingest):
    _write(ingest / "good.epub")
    _write(ingest / "readme.txt")
    assert bs_main._ingest_ebook_files() == ["good.epub"]


def test_ingest_files_with_exclude_set(ingest):
    _write(ingest / "orphan.epub")
    _write(ingest / "fresh.epub")
    files = bs_main._ingest_ebook_files(exclude={"orphan.epub"})
    assert files == ["fresh.epub"]


def test_sweep_orphans_deletes_mismatch_and_tiny(ingest):
    _write(ingest / "good.epub")
    _write(ingest / "rate-limit.MISMATCH.epub", b"too many downloads")
    _write(ingest / "stub.epub", b"<html>err</html>")
    removed = bs_main._sweep_ingest_orphans()
    assert sorted(removed) == ["rate-limit.MISMATCH.epub", "stub.epub"]
    assert sorted(os.listdir(ingest)) == ["good.epub"]


def test_sweep_orphans_handles_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_main, "CWA_INGEST_PATH", str(tmp_path / "does-not-exist"))
    assert bs_main._sweep_ingest_orphans() == []


@pytest.mark.asyncio
async def test_wait_for_file_returns_only_new_arrivals(ingest):
    _write(ingest / "orphan.epub")
    snapshot = set(os.listdir(ingest))

    async def add_later():
        await asyncio.sleep(0.5)
        _write(ingest / "fresh.epub")

    asyncio.create_task(add_later())
    result = await bs_main._wait_for_file_in_ingest(timeout=10, exclude=snapshot, poll_interval=1)
    assert result == "fresh.epub"


@pytest.mark.asyncio
async def test_wait_for_file_ignores_pre_existing_orphans(ingest):
    _write(ingest / "orphan.epub")
    snapshot = set(os.listdir(ingest))

    result = await bs_main._wait_for_file_in_ingest(timeout=2, exclude=snapshot, poll_interval=1)
    assert result is None


@pytest.mark.asyncio
async def test_wait_for_file_always_skips_mismatch_files(ingest):
    async def add_garbage():
        await asyncio.sleep(0.5)
        _write(ingest / "garbage.MISMATCH.epub", b"too many downloads")

    asyncio.create_task(add_garbage())
    result = await bs_main._wait_for_file_in_ingest(timeout=4, poll_interval=1)
    assert result is None


@pytest.mark.asyncio
async def test_wait_for_file_or_stacks_fail_respects_exclude(ingest):
    _write(ingest / "orphan.epub")
    snapshot = set(os.listdir(ingest))

    async def add_later():
        await asyncio.sleep(0.5)
        _write(ingest / "fresh.epub")

    asyncio.create_task(add_later())
    bs_main.annas_scraper = None  # short-circuit Stacks status check
    appeared, stacks_failed = await bs_main._wait_for_file_in_ingest_or_stacks_fail(
        "deadbeef", timeout=10, exclude=snapshot, poll_interval=1
    )
    assert appeared == "fresh.epub"
    assert stacks_failed is False


def test_invalid_reason_flags_mismatch(tmp_path):
    p = tmp_path / "x.MISMATCH.epub"
    p.write_bytes(b"x" * 100_000)
    assert "MISMATCH" in (bs_main._invalid_ingest_reason(p.name, str(p)) or "")


def test_invalid_reason_flags_tiny(tmp_path):
    p = tmp_path / "x.epub"
    p.write_bytes(b"too small")
    reason = bs_main._invalid_ingest_reason(p.name, str(p)) or ""
    assert "size" in reason.lower() or "small" in reason.lower()


def test_invalid_reason_passes_valid_file(tmp_path):
    p = tmp_path / "x.epub"
    p.write_bytes(b"x" * 100_000)
    assert bs_main._invalid_ingest_reason(p.name, str(p)) is None


def test_publish_ingest_file_uses_atomic_rename(ingest, monkeypatch):
    observed = {}
    real_replace = os.replace

    def inspect_replace(source, destination):
        observed["source"] = source
        observed["destination"] = destination
        observed["destination_existed"] = os.path.exists(destination)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect_replace)

    destination = bs_main._publish_ingest_file(b"complete ebook", "book.epub")

    assert observed["source"].endswith(".part")
    assert observed["destination"] == str(ingest / "book.epub")
    assert observed["destination_existed"] is False
    assert destination == str(ingest / "book.epub")
    assert (ingest / "book.epub").read_bytes() == b"complete ebook"
    assert not os.path.exists(observed["source"])


def test_prepare_audiobook_save_path_creates_writable_destination(tmp_path):
    destination = tmp_path / "Ray Dalio" / "How Countries Go Broke"

    bs_main._prepare_audiobook_save_path(str(destination))

    assert destination.is_dir()
    assert destination.stat().st_mode & 0o700 == 0o700
