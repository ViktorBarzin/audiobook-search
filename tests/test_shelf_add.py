"""Tests for putting an imported book on a calibre-web shelf.

Verified live against CWA on 2026-08-16: `POST /shelf/add/<shelf>/<book>` answers
**204 No Content** on success, not 200. Treating only 200 as success would have
reported every successful shelving as a failure.
"""

import httpx

from backend import main as bs_main

CSRF_PAGE = '<html><input name="csrf_token" value="tok123"></html>'


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def test_success_returns_no_error_on_204():
    def handler(request):
        if request.url.path.endswith("/shelf/add/6/501"):
            return httpx.Response(204)
        return httpx.Response(200, text=CSRF_PAGE)

    async with client_for(handler) as client:
        assert await bs_main._add_to_shelf(client, 6, 501) is None


async def test_success_also_accepted_on_200():
    def handler(request):
        if "/shelf/add/" in request.url.path:
            return httpx.Response(200)
        return httpx.Response(200, text=CSRF_PAGE)

    async with client_for(handler) as client:
        assert await bs_main._add_to_shelf(client, 6, 501) is None


async def test_already_on_shelf_is_not_an_error():
    """Re-shelving must be harmless so the pipeline can retry safely."""
    def handler(request):
        if "/shelf/add/" in request.url.path:
            return httpx.Response(400, text="Book is already part of the shelf: Goodreads wishlist")
        return httpx.Response(200, text=CSRF_PAGE)

    async with client_for(handler) as client:
        assert await bs_main._add_to_shelf(client, 6, 501) is None


async def test_permission_denied_is_reported():
    def handler(request):
        if "/shelf/add/" in request.url.path:
            return httpx.Response(403, text="Sorry you are not allowed to add a book to that shelf")
        return httpx.Response(200, text=CSRF_PAGE)

    async with client_for(handler) as client:
        error = await bs_main._add_to_shelf(client, 6, 501)

    assert error is not None
    assert "403" in error


async def test_missing_csrf_token_is_reported():
    async with client_for(lambda r: httpx.Response(200, text="<html>no token</html>")) as client:
        assert await bs_main._add_to_shelf(client, 6, 501) == "no CSRF token"
