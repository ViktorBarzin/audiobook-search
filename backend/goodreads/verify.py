"""Claude's second opinion on a Goodreads match, before and after download.

The matcher compares strings, strictly, and cannot tell a study guide from the
novel it discusses when the two share a title line. Nothing reviews a pick
before it reaches Anca's Kindle, so Claude reads what the matcher cannot: the
record (title, author, publisher, year, language) before anything is fetched,
and the file's own metadata and opening pages after. Only a plain "yes" lets a
book through; "no" and "unsure" both refuse it, which keeps the pipeline's
"skip on doubt" rule.

Claude is reached through claude-agent-service's OpenAI-compatible endpoint,
which answers synchronously. An outage or an unreadable reply raises
VerifierUnavailable, never a verdict, so a book is delayed rather than spent.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from backend.goodreads.sources import SourceUnavailable

logger = logging.getLogger(__name__)

MODEL = "sonnet"
# The service queues requests behind other agents' work, so allow for a wait.
TIMEOUT_SECONDS = 300
# Enough for a title page, a copyright page and the start of chapter one.
MAX_TEXT_CHARS = 8_000
MAX_DESCRIPTION_CHARS = 1_200
PDF_PAGES = 8

SYSTEM_PROMPT = """\
You check whether an ebook is the book a reader asked for. Reply with one JSON \
object and nothing else: {"verdict": "yes" | "no" | "unsure", "reason": "<one short sentence>"}.

Say "yes" only when it is the same work by the same author, and the same volume \
or instalment when the book belongs to a series. Another edition, printing or \
publisher of that work is fine, and so is an English translation.

Say "no" for a different work, a different volume, an omnibus or collection \
when one book was asked for (or one book when a collection was asked for), a \
summary, study guide, workbook, excerpt or sample, and for any text that is not \
in English.

Say "unsure" when the evidence is not enough to tell."""


class VerifierUnavailable(SourceUnavailable):
    """Claude could not give a verdict: the book waits for a later cycle."""


@dataclass
class Verdict:
    ok: bool
    reason: str


@dataclass
class WantedBook:
    """The book as Goodreads describes it."""

    title: str
    author: str | None
    isbn: str | None = None
    published: str | None = None
    description: str | None = None


@dataclass
class OfferedFile:
    """The file as libgen describes it."""

    title: str
    author: str | None
    publisher: str | None = None
    year: str | None = None
    language: str | None = None
    ext: str | None = None
    size_bytes: int | None = None


@dataclass
class FileEvidence:
    """What a downloaded file says about itself."""

    format: str
    title: str | None = None
    author: str | None = None
    language: str | None = None
    identifiers: list[str] = field(default_factory=list)
    text: str = ""


# --------------------------------------------------------------------------- #
# Prompts and verdicts                                                         #
# --------------------------------------------------------------------------- #

def _lines(pairs) -> str:
    return "\n".join(f"{label}: {value}" for label, value in pairs if value)


def _wanted_block(wanted: WantedBook) -> str:
    description = (wanted.description or "")[:MAX_DESCRIPTION_CHARS]
    return "WANTED (from the reader's Goodreads shelf)\n" + _lines([
        ("Title", wanted.title), ("Author", wanted.author), ("ISBN", wanted.isbn),
        ("First published", wanted.published), ("Description", description),
    ])


def _offered_block(offered: OfferedFile) -> str:
    size = f"{offered.size_bytes / 1_000_000:.1f} MB" if offered.size_bytes else None
    return "OFFERED (the library record for a downloadable file)\n" + _lines([
        ("Title", offered.title), ("Author", offered.author),
        ("Publisher", offered.publisher), ("Year", offered.year),
        ("Language", offered.language or "not stated"), ("Format", offered.ext),
        ("Size", size),
    ])


def _evidence_block(evidence: FileEvidence) -> str:
    return f"THE FILE ITSELF ({evidence.format})\n" + _lines([
        ("Embedded title", evidence.title), ("Embedded author", evidence.author),
        ("Embedded language", evidence.language),
        ("Embedded identifiers", ", ".join(evidence.identifiers)),
    ]) + f"\nOpening text:\n{evidence.text or '(no readable text)'}"


def record_prompt(wanted: WantedBook, offered: OfferedFile) -> str:
    return (f"{_wanted_block(wanted)}\n\n{_offered_block(offered)}\n\n"
            "Is the offered file the wanted book?")


def file_prompt(wanted: WantedBook, offered: OfferedFile, evidence: FileEvidence) -> str:
    return (f"{_wanted_block(wanted)}\n\n{_offered_block(offered)}\n\n"
            f"{_evidence_block(evidence)}\n\n"
            "The file has been downloaded. Judge by what the file itself contains: "
            "is it the wanted book, in English?")


_OBJECT_RE = re.compile(r"\{.*\}", re.S)


def parse_verdict(text: str) -> Verdict:
    """Read Claude's JSON answer. Anything unreadable is an outage, not a 'no'."""
    match = _OBJECT_RE.search(text or "")
    if not match:
        raise VerifierUnavailable(f"unreadable reply: {(text or '')[:120]!r}")
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VerifierUnavailable(f"unreadable reply: {exc}") from exc
    verdict = str(obj.get("verdict", "")).strip().lower() if isinstance(obj, dict) else ""
    if verdict not in ("yes", "no", "unsure"):
        raise VerifierUnavailable(f"reply without a verdict: {match.group(0)[:120]!r}")
    reason = str(obj.get("reason") or verdict).strip()
    return Verdict(ok=verdict == "yes", reason=reason if verdict != "unsure"
                   else f"Claude was unsure: {reason}")


# --------------------------------------------------------------------------- #
# The client                                                                   #
# --------------------------------------------------------------------------- #

class ClaudeVerifier:
    def __init__(self, url: str, token: str, model: str = MODEL,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.model = model
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def check_record(self, wanted: WantedBook, offered: OfferedFile) -> Verdict:
        return await self._ask(record_prompt(wanted, offered))

    async def check_file(self, wanted: WantedBook, offered: OfferedFile,
                         evidence: FileEvidence) -> Verdict:
        return await self._ask(file_prompt(wanted, offered, evidence))

    async def _ask(self, prompt: str) -> Verdict:
        if not self.configured:
            raise VerifierUnavailable("Claude check is not configured")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS,
                                         transport=self._transport) as client:
                response = await client.post(
                    f"{self.url}/v1/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
        except httpx.HTTPError as exc:
            raise VerifierUnavailable(f"claude-agent-service unreachable: "
                                      f"{type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise VerifierUnavailable(
                f"claude-agent-service answered {response.status_code}: {response.text[:200]}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VerifierUnavailable(f"unexpected response shape: {exc}") from exc
        verdict = parse_verdict(str(content))
        logger.info("Claude verdict: %s (%s)", "yes" if verdict.ok else "no", verdict.reason)
        return verdict


# --------------------------------------------------------------------------- #
# Reading a downloaded file                                                    #
# --------------------------------------------------------------------------- #

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _html_text(markup: bytes) -> str:
    text = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def _epub_evidence(data: bytes) -> FileEvidence | None:
    with zipfile.ZipFile(io.BytesIO(data)) as epub:
        container = ET.fromstring(epub.read("META-INF/container.xml"))
        opf_path = next((el.get("full-path") for el in container.iter()
                         if _local(el.tag) == "rootfile"), None)
        if not opf_path:
            return None
        opf = ET.fromstring(epub.read(opf_path))
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""

        def first(name):
            return next(((el.text or "").strip() for el in opf.iter()
                         if _local(el.tag) == name and (el.text or "").strip()), None)

        identifiers = [(el.text or "").strip() for el in opf.iter()
                       if _local(el.tag) == "identifier" and (el.text or "").strip()]
        manifest = {el.get("id"): el.get("href") for el in opf.iter()
                    if _local(el.tag) == "item"}
        spine = [el.get("idref") for el in opf.iter() if _local(el.tag) == "itemref"]

        text = ""
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            try:
                text += " " + _html_text(epub.read(base + href))
            except KeyError:
                continue
            if len(text) >= MAX_TEXT_CHARS:
                break

    return FileEvidence(format="epub", title=first("title"), author=first("creator"),
                        language=first("language"), identifiers=identifiers,
                        text=text.strip()[:MAX_TEXT_CHARS])


def _fb2_evidence(data: bytes) -> FileEvidence | None:
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = next((n for n in archive.namelist() if n.lower().endswith(".fb2")), None)
            if not name:
                return None
            data = archive.read(name)
    root = ET.fromstring(data)

    def find(name, within=None):
        return next((el for el in (within if within is not None else root).iter()
                     if _local(el.tag) == name), None)

    info = find("title-info")
    title = author = language = None
    if info is not None:
        title_el, lang_el, author_el = find("book-title", info), find("lang", info), find("author", info)
        title = (title_el.text or "").strip() if title_el is not None else None
        language = (lang_el.text or "").strip() if lang_el is not None else None
        if author_el is not None:
            parts = [(el.text or "").strip() for el in author_el
                     if _local(el.tag) in ("first-name", "middle-name", "last-name")]
            author = " ".join(p for p in parts if p) or None
    body = find("body")
    text = re.sub(r"\s+", " ", " ".join(body.itertext())).strip() if body is not None else ""
    return FileEvidence(format="fb2", title=title, author=author, language=language,
                        text=text[:MAX_TEXT_CHARS])


def _pdf_evidence(data: bytes) -> FileEvidence | None:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    meta = reader.metadata or {}
    text = ""
    for page in reader.pages[:PDF_PAGES]:
        text += " " + (page.extract_text() or "")
        if len(text) >= MAX_TEXT_CHARS:
            break
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        # A scanned pdf has no text layer; the record check is all we have.
        return None
    return FileEvidence(format="pdf", title=getattr(meta, "title", None),
                        author=getattr(meta, "author", None), text=text[:MAX_TEXT_CHARS])


_READERS = {"epub": _epub_evidence, "fb2": _fb2_evidence, "pdf": _pdf_evidence}


def extract_evidence(data: bytes, ext: str | None) -> FileEvidence | None:
    """What the file says about itself, or None when it cannot be read.

    mobi and azw3 are not read (their container needs a heavier parser), and a
    file that fails to parse gives nothing either: for those the pre-download
    record check is the only check, as agreed on 2026-09-27.
    """
    reader = _READERS.get((ext or "").lower().lstrip("."))
    if not reader:
        return None
    try:
        return reader(data)
    except Exception as exc:
        logger.info("Could not read the %s for the Claude check: %s", ext, exc)
        return None


def wanted_from(title: str, author: str | None, details: dict) -> WantedBook:
    return WantedBook(title=title, author=author, isbn=details.get("isbn"),
                      published=details.get("published"),
                      description=details.get("description"))


def offered_from(details: dict) -> OfferedFile:
    c = details.get("candidate") or {}
    return OfferedFile(title=c.get("title") or "", author=c.get("author"),
                       publisher=c.get("publisher"), year=c.get("year"),
                       language=c.get("language"), ext=c.get("ext"),
                       size_bytes=c.get("size_bytes"))
