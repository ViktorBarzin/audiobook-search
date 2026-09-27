"""Replay the matcher over real shelf items and print what it would do.

This is the go-live gate: the pipeline has no review step, so the matcher's picks
and rejections are checked by hand once, over a real sample, before downloads are
switched on. Nothing here writes to Calibre, Slack or the database.

    python -m backend.goodreads.replay --limit 50
    python -m backend.goodreads.replay --limit 50 --verify --json

--verify runs the live gate too: Claude checks each ranked candidate's record,
the file is downloaded into memory, Claude checks its contents, and the first
candidate passing both is the pick. Files are never written or imported. It
needs CLAUDE_AGENT_URL and CLAUDE_AGENT_TOKEN, so run it in the book-search pod.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

from backend.goodreads.feed import fetch_shelf
from backend.goodreads.matcher import is_placeholder, rank_candidates, select_candidate
from backend.goodreads.sources import SourceUnavailable
from backend.goodreads.sync import MAX_CANDIDATES, search_queries, verify_details
from backend.goodreads.verify import (
    ClaudeVerifier,
    VerifierUnavailable,
    extract_evidence,
    offered_from,
    wanted_from,
)
from backend.libgen import LibGenScraper

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


async def run_gate(item, candidates, isbn_md5s, source, verifier) -> dict:
    """Walk the ranked candidates through both Claude checks, as the endpoint does."""
    steps = []
    for candidate, reason in rank_candidates(item, candidates, isbn_md5s)[:MAX_CANDIDATES]:
        details = verify_details(item, candidate)
        wanted, offered = wanted_from(item.title, item.author, details), offered_from(details)
        step = {"md5": candidate.md5, "title": candidate.title, "author": candidate.author,
                "ext": candidate.ext, "language": candidate.language, "matched_by": reason}
        steps.append(step)
        try:
            verdict = await verifier.check_record(wanted, offered)
            step["record"] = ("yes: " if verdict.ok else "no: ") + verdict.reason
            if not verdict.ok:
                continue
            data, filename = await source.download_file(candidate.md5)
            if not data:
                step["file"] = "download failed"
                continue
            ext = filename.rsplit(".", 1)[-1] if filename and "." in filename else candidate.ext
            evidence = extract_evidence(data, ext)
            del data
            if evidence is None:
                step["file"] = f"not readable ({ext}); record check only"
                return {"accepted": step, "steps": steps}
            step["file_says"] = f"{evidence.title} / {evidence.author} / {evidence.language}"
            verdict = await verifier.check_file(wanted, offered, evidence)
            step["file"] = ("yes: " if verdict.ok else "no: ") + verdict.reason
            if verdict.ok:
                return {"accepted": step, "steps": steps}
        except VerifierUnavailable as exc:
            step["error"] = f"Claude unavailable: {exc}"
            return {"accepted": None, "steps": steps, "unavailable": True}
    return {"accepted": None, "steps": steps}


async def replay(user_id: str, shelf: str, limit: int, as_json: bool,
                 verify: bool = False) -> int:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        feed = await fetch_shelf(client, user_id, shelf, per_page=100)

    items = feed.items[:limit]
    source = LibGenScraper()
    verifier = ClaudeVerifier(os.getenv("CLAUDE_AGENT_URL", ""),
                              os.getenv("CLAUDE_AGENT_TOKEN", "")) if verify else None
    if verify and not verifier.configured:
        print("--verify needs CLAUDE_AGENT_URL and CLAUDE_AGENT_TOKEN", file=sys.stderr)
        return 2
    rows = []

    for item in items:
        candidates, isbn_md5s, unavailable = [], set(), False
        if not is_placeholder(item.title):
            try:
                if item.isbn:
                    by_isbn = await source.search_by_isbn(item.isbn)
                    candidates.extend(by_isbn)
                    isbn_md5s = {c.md5 for c in by_isbn}
                for query in search_queries(item):
                    candidates.extend(await source.search_candidates(query))
                    if candidates:
                        break
            except SourceUnavailable as exc:
                # In production this defers the book to the next cycle rather than
                # spending its one attempt; here it is just reported.
                unavailable = True
                print(f"  source unavailable for {item.title!r}: {exc}", file=sys.stderr)

        deduped = {}
        for candidate in candidates:
            deduped.setdefault(candidate.md5, candidate)
        candidates = list(deduped.values())

        match = select_candidate(item, candidates, isbn_matched_md5s=isbn_md5s)
        if unavailable and match.candidate is None:
            match = type(match)(None, "source_unavailable")
        gate = None
        if verifier and match.candidate:
            gate = await run_gate(item, candidates, isbn_md5s, source, verifier)
            print(f"  gate {item.title[:40]!r}: "
                  f"{'ACCEPT ' + gate['accepted']['md5'][:8] if gate['accepted'] else 'REFUSE'}",
                  file=sys.stderr)
        rows.append({
            "gate": gate,
            "goodreads_title": item.title,
            "goodreads_author": item.author,
            "isbn": item.isbn,
            "candidates_seen": len(candidates),
            "decision": "DOWNLOAD" if match.candidate else "SKIP",
            "reason": match.reason,
            "picked_title": match.candidate.title if match.candidate else None,
            "picked_author": match.candidate.author if match.candidate else None,
            "picked_ext": match.candidate.ext if match.candidate else None,
            "picked_language": match.candidate.language if match.candidate else None,
            "picked_md5": match.candidate.md5 if match.candidate else None,
        })

    await source.close()

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0

    picked = sum(1 for r in rows if r["decision"] == "DOWNLOAD")
    print(f"\nReplayed {len(rows)} shelf items — {picked} would download, "
          f"{len(rows) - picked} would be skipped\n")
    print(f"{'GOODREADS SAYS':<46} {'DECISION':<9} {'WOULD FETCH':<42} WHY")
    print("─" * 132)
    for row in rows:
        colour = GREEN if row["decision"] == "DOWNLOAD" else (
            DIM if row["reason"] in ("placeholder_title", "not_found") else YELLOW
        )
        wanted = f"{row['goodreads_title'][:30]} — {row['goodreads_author'][:12]}"
        got = "—"
        if row["picked_title"]:
            got = (f"{row['picked_title'][:26]} [{row['picked_ext']},"
                   f"{(row['picked_language'] or '?')[:7]}]")
        print(f"{colour}{wanted:<46} {row['decision']:<9} {got:<42} {row['reason']}{RESET}")

    print(f"\n{DIM}Check every DOWNLOAD row: is the fetched book the book she shelved?{RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="33074940")
    parser.add_argument("--shelf", default="to-read")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verify", action="store_true",
                        help="run the Claude checks and download picks into memory")
    args = parser.parse_args()
    return asyncio.run(replay(args.user_id, args.shelf, args.limit, args.json, args.verify))


if __name__ == "__main__":
    sys.exit(main())
