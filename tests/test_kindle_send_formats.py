"""Sending a book to a Kindle in a chosen format only.

The manual paths still try epub then fall back to pdf; the automatic Goodreads
path pins the format it already decided on, so it must not quietly fall back to
something it deliberately ruled out.
"""

import httpx
import pytest

from backend import main as bs_main


@pytest.fixture
def smtp_ready(monkeypatch):
    """Make the send reach the point of building a message, without sending one."""
    monkeypatch.setattr(bs_main, "SMTP_USER", "calibre-web@viktorbarzin.me")
    monkeypatch.setattr(bs_main, "SMTP_PASS", "secret")
    sent = []
    monkeypatch.setattr(bs_main, "_smtp_send_with_retry", lambda msg: sent.append(msg))
    return sent


def opds_client_factory(monkeypatch, handler):
    """Point _send_to_kindle's internal client at a mock transport."""
    requested = []

    def record(request):
        requested.append(request.url.path)
        return handler(request)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real(transport=httpx.MockTransport(record), follow_redirects=True)

    monkeypatch.setattr(bs_main.httpx, "AsyncClient", factory)
    return requested


async def test_a_pinned_format_is_the_only_one_fetched(monkeypatch, smtp_ready):
    def handler(request):
        if "/opds/download/501/epub/" in request.url.path:
            return httpx.Response(200, content=b"E" * 5000)
        return httpx.Response(404)

    requested = opds_client_factory(monkeypatch, handler)

    error = await bs_main._send_to_kindle(
        501, "Neuromancer", "anca@kindle.com", formats=("epub",),
    )

    assert error is None
    assert len(smtp_ready) == 1
    assert all("pdf" not in path for path in requested), requested


async def test_a_pinned_format_that_is_missing_does_not_fall_back(monkeypatch, smtp_ready):
    """Only a pdf is on disk, but epub was pinned: nothing should be sent."""
    def handler(request):
        if "/opds/download/501/pdf/" in request.url.path:
            return httpx.Response(200, content=b"P" * 5000)
        return httpx.Response(404)

    requested = opds_client_factory(monkeypatch, handler)

    error = await bs_main._send_to_kindle(
        501, "Neuromancer", "anca@kindle.com", formats=("epub",),
    )

    assert error and "no downloadable format" in error.lower()
    assert smtp_ready == []
    assert all("pdf" not in path for path in requested), requested


async def test_the_default_still_tries_epub_then_pdf(monkeypatch, smtp_ready):
    """The manual send-to-kindle route must keep its pdf fallback."""
    def handler(request):
        if "/opds/download/501/pdf/" in request.url.path:
            return httpx.Response(200, content=b"P" * 5000)
        return httpx.Response(404)

    requested = opds_client_factory(monkeypatch, handler)

    error = await bs_main._send_to_kindle(501, "Some Manual", "anca@kindle.com")

    assert error is None
    assert len(smtp_ready) == 1
    assert any("epub" in path for path in requested)
    assert any("pdf" in path for path in requested)


async def test_an_oversized_book_is_refused_before_the_relay_bounces_it(
    monkeypatch, smtp_ready,
):
    """Brevo rejects a message over 20 MiB with dsn=5.3.4, and the sending app
    only sees a 200 — so the size has to be caught here to be visible at all."""
    from backend.kindle import MAX_BOOK_BYTES

    def handler(request):
        if "/opds/download/501/epub/" in request.url.path:
            return httpx.Response(200, content=b"E" * (MAX_BOOK_BYTES + 1))
        return httpx.Response(404)

    opds_client_factory(monkeypatch, handler)

    error = await bs_main._send_to_kindle(
        501, "Strange Houses", "anca@kindle.com", formats=("epub",),
    )

    assert error and "too large" in error.lower()
    assert smtp_ready == [], "nothing should reach SMTP"


async def test_a_book_at_the_limit_is_still_sent(monkeypatch, smtp_ready):
    from backend.kindle import MAX_BOOK_BYTES

    def handler(request):
        if "/opds/download/501/epub/" in request.url.path:
            return httpx.Response(200, content=b"E" * MAX_BOOK_BYTES)
        return httpx.Response(404)

    opds_client_factory(monkeypatch, handler)

    error = await bs_main._send_to_kindle(
        501, "Just Under", "anca@kindle.com", formats=("epub",),
    )

    assert error is None
    assert len(smtp_ready) == 1
