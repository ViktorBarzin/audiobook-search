"""Anna's Archive, searched through the cluster's headful Chrome.

AA sits behind DDoS-Guard, which refuses our HTTP clients outright. Measured
2026-08-16, all from the same home IP that works fine in a human's browser:

- `httpx` direct → 403, even replaying a human-solved session's `__ddg2_`
  cookies with a matching Chrome user-agent, so the check is on the TLS/HTTP
  fingerprint rather than the session.
- FlareSolverr → solves Cloudflare, not DDoS-Guard.
- NordVPN UK egress → no change.
- Playwright-driven Chrome → challenged, even in the browser holding the solved
  session.

What does work is the shared cluster Chrome (`chrome-service`) once a human has
passed the captcha in it: after that, plain CDP navigation to `/search` returns
real results. So this source drives that browser, in its own tab, and treats a
lapsed session as an outage rather than an empty result — an empty list would
read as "this book does not exist" and spend the book's single attempt.

The session is human-maintained: when DDoS-Guard next challenges, AA quietly
drops out until someone solves it again, and the pipeline runs on libgen alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request

from backend.goodreads.isbn import to_isbn13
from backend.goodreads.matcher import Candidate
from backend.goodreads.sources import SourceUnavailable

logger = logging.getLogger(__name__)

CDP_URL = os.getenv("CHROME_CDP_URL", "http://chrome-service.chrome-service.svc.cluster.local:9222")
AA_DOMAIN = os.getenv("ANNAS_SEARCH_DOMAIN", "annas-archive.pk")
PAGE_SETTLE_SECONDS = float(os.getenv("ANNAS_PAGE_SETTLE_SECONDS", "12"))

_MD5_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
_EXT_RE = re.compile(r"\.(epub|azw3|mobi|fb2|pdf)\b", re.IGNORECASE)

# Reads every /md5/ anchor with the text around it. Sidebar links come through
# too; the matcher's title+author rules are what separate them from real hits,
# which is safer than guessing at AA's result-container markup.
EXTRACT_JS = r"""JSON.stringify({
  title: document.title,
  rows: [...document.querySelectorAll('a[href^="/md5/"]')].slice(0, 60).map(a => {
    let box = a;
    for (let i = 0; i < 4 && box.parentElement; i++) box = box.parentElement;
    return {
      md5: a.getAttribute('href').split('/')[2],
      lines: (box.innerText || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 4)
    };
  })
})"""


def parse_rows(rows: list[dict]) -> list[Candidate]:
    """Turn extracted anchors into Candidates.

    An AA row reads: a file path (which carries the extension), the title, the
    authors, then publisher and blurb. Size is not exposed per row, and None is
    accepted by the matcher's size floor.
    """
    candidates: list[Candidate] = []

    for row in rows:
        md5 = (row.get("md5") or "").lower()
        if not _MD5_RE.match(md5):
            continue

        lines = [ln for ln in (row.get("lines") or []) if ln]
        if not lines:
            continue

        path_line = lines[0] if _EXT_RE.search(lines[0] or "") else ""
        rest = [ln for ln in lines if ln != path_line]
        if not rest:
            continue

        ext_match = _EXT_RE.search(path_line)
        candidates.append(Candidate(
            md5=md5,
            title=rest[0],
            author=rest[1] if len(rest) > 1 else None,
            ext=(ext_match.group(1).lower() if ext_match else "epub"),
            # AA rows carry no language; the query pins lang=en.
            language="English",
            size_bytes=None,
            source="annas",
        ))

    return candidates


async def _cdp_evaluate(url: str, js: str) -> dict:
    """Open our own tab in the shared browser, read a page, close the tab."""
    import websockets

    def _http(path: str, method: str = "GET"):
        request = urllib.request.Request(f"{CDP_URL}{path}", method=method)
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode()
        return json.loads(body) if body.strip().startswith(("{", "[")) else {}

    tab = await asyncio.to_thread(_http, "/json/new?about:blank", "PUT")
    tab_id = tab.get("id")
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            counter = 0

            async def evaluate(expression: str):
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({
                    "id": counter, "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True,
                               "awaitPromise": True, "userGesture": True},
                }))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == counter:
                        return message.get("result", {}).get("result", {}).get("value")

            await evaluate(f"location.href={json.dumps(url)}")
            await asyncio.sleep(PAGE_SETTLE_SECONDS)
            return json.loads(await evaluate(EXTRACT_JS) or "{}")
    finally:
        if tab_id:
            try:
                await asyncio.to_thread(_http, f"/json/close/{tab_id}")
            except Exception as exc:
                logger.warning("Could not close CDP tab %s: %s", tab_id, exc)


class AnnasSource:
    """Search Anna's Archive through the shared headful browser."""

    def __init__(self, evaluator=None):
        self._evaluate = evaluator or _cdp_evaluate

    async def close(self):
        return None

    async def search_by_isbn(self, isbn: str | None) -> list[Candidate]:
        isbn13 = to_isbn13(isbn)
        if not isbn13:
            return []
        return await self._search(isbn13)

    async def search_candidates(self, query: str) -> list[Candidate]:
        return await self._search(query)

    async def _search(self, query: str) -> list[Candidate]:
        from urllib.parse import quote_plus

        url = (f"https://{AA_DOMAIN}/search?q={quote_plus(query)}"
               "&content=book_nonfiction&content=book_fiction&ext=epub&lang=en")

        try:
            page = await self._evaluate(url, EXTRACT_JS)
        except Exception as exc:
            raise SourceUnavailable(f"cluster browser unreachable: {exc}") from exc

        title = (page or {}).get("title") or ""
        if "ddos-guard" in title.lower():
            raise SourceUnavailable(
                "Anna's Archive is showing its captcha again — the shared browser "
                "session needs a human to pass it before AA can be searched"
            )

        return parse_rows((page or {}).get("rows") or [])
