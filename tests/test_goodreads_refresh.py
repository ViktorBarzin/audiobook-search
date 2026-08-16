"""Deferred work must resume even when the shelf is quiet.

Live 2026-08-16: a book was left claimed by a pod that died mid-download. The
claim expires after 15 minutes and the book should have been retried — but the
poller only re-examines items on a cycle that returns 200, and an unchanged
shelf answers 304 forever. The row sat untouched. Dropping the ETag
periodically is what lets pending books and abandoned claims come back.
"""

import pytest

from backend.goodreads.runner import FULL_REFRESH_SECONDS, etag_to_send


def test_sends_the_etag_during_the_quiet_window():
    assert etag_to_send('W/"abc"', last_full=1000.0, now=1060.0) == 'W/"abc"'


def test_drops_the_etag_once_the_refresh_interval_passes():
    later = 1000.0 + FULL_REFRESH_SECONDS
    assert etag_to_send('W/"abc"', last_full=1000.0, now=later) is None


def test_drops_the_etag_well_past_the_interval():
    assert etag_to_send('W/"abc"', last_full=0.0, now=99_999.0) is None


def test_no_etag_yet_is_always_a_full_fetch():
    assert etag_to_send(None, last_full=1000.0, now=1001.0) is None


@pytest.mark.parametrize("elapsed,expected_full", [
    (0, False), (60, False), (FULL_REFRESH_SECONDS - 1, False),
    (FULL_REFRESH_SECONDS, True), (FULL_REFRESH_SECONDS * 3, True),
])
def test_refresh_boundary(elapsed, expected_full):
    sent = etag_to_send('W/"abc"', last_full=500.0, now=500.0 + elapsed)
    assert (sent is None) is expected_full
