"""Claude as a second opinion on a match, before and after the download.

The matcher's rules are strict but blind to meaning: they cannot tell a study
guide from the novel it is about when the title line is the same. Claude reads
the record and then the file, and only a clear "yes" lets a book through.
"""

import io
import json
import zipfile

import httpx
import pytest

from backend.goodreads.verify import (
    ClaudeVerifier,
    OfferedFile,
    VerifierUnavailable,
    WantedBook,
    extract_evidence,
    parse_verdict,
)

WANTED = WantedBook(
    title="Romanov", author="Nadine Brandes", isbn="0785217240",
    published="2019", description="The history books say I died.",
)
OFFERED = OfferedFile(
    title="Romanov", author="Nadine Brandes", publisher="Thomas Nelson",
    year="2019", language="English", ext="epub", size_bytes=2_400_000,
)


# --------------------------------------------------------------------------- #
# Reading Claude's answer                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, ok", [
    ('{"verdict": "yes", "reason": "same novel"}', True),
    ('```json\n{"verdict": "yes", "reason": "same novel"}\n```', True),
    ('Here you go: {"verdict": "YES", "reason": "same"}', True),
    ('{"verdict": "no", "reason": "a study guide"}', False),
    ('{"verdict": "unsure", "reason": "no text"}', False),
])
def test_only_a_clear_yes_lets_a_book_through(text, ok):
    assert parse_verdict(text).ok is ok


def test_the_reason_is_kept_for_the_slack_line():
    verdict = parse_verdict('{"verdict": "no", "reason": "a study guide, not the novel"}')

    assert verdict.reason == "a study guide, not the novel"


@pytest.mark.parametrize("text", ["", "I think so", '{"reason": "x"}', '{"verdict": "maybe"}'])
def test_an_answer_that_cannot_be_read_is_an_outage_not_a_verdict(text):
    """Burning one of a book's candidates on a garbled reply would be the
    pipeline's fault, not the file's, so it is retried like any outage."""
    with pytest.raises(VerifierUnavailable):
        parse_verdict(text)


# --------------------------------------------------------------------------- #
# Talking to claude-agent-service                                              #
# --------------------------------------------------------------------------- #

def verifier_answering(handler):
    return ClaudeVerifier(
        url="http://claude-agent-service", token="secret",
        transport=httpx.MockTransport(handler),
    )


def completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_the_record_check_sends_both_sides_and_reads_the_verdict():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return completion('{"verdict": "yes", "reason": "same novel"}')

    verdict = await verifier_answering(handler).check_record(WANTED, OFFERED)

    assert verdict.ok
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    prompt = seen["body"]["messages"][-1]["content"]
    for fact in ("Romanov", "Nadine Brandes", "0785217240", "Thomas Nelson", "English",
                 "The history books say I died."):
        assert fact in prompt


async def test_the_file_check_includes_what_the_file_says_about_itself():
    seen = {}

    def handler(request):
        seen["prompt"] = json.loads(request.content)["messages"][-1]["content"]
        return completion('{"verdict": "no", "reason": "this is a different book"}')

    evidence = extract_evidence(make_epub("Romanov", "Nadine Brandes",
                                          "Chapter One. Ekaterinburg, 1918."), "epub")
    verdict = await verifier_answering(handler).check_file(WANTED, OFFERED, evidence)

    assert not verdict.ok
    assert verdict.reason == "this is a different book"
    assert "Ekaterinburg, 1918." in seen["prompt"]


@pytest.mark.parametrize("response", [
    httpx.Response(503, json={"error": "execution failed", "detail": "queue full"}),
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"unexpected": True}),
])
async def test_a_service_that_does_not_answer_is_unavailable(response):
    with pytest.raises(VerifierUnavailable):
        await verifier_answering(lambda request: response).check_record(WANTED, OFFERED)


async def test_a_connection_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(VerifierUnavailable):
        await verifier_answering(handler).check_record(WANTED, OFFERED)


async def test_an_unconfigured_verifier_refuses_rather_than_waving_books_through():
    verifier = ClaudeVerifier(url="", token="")

    assert not verifier.configured
    with pytest.raises(VerifierUnavailable):
        await verifier.check_record(WANTED, OFFERED)


# --------------------------------------------------------------------------- #
# Reading a file's own words                                                   #
# --------------------------------------------------------------------------- #

def make_epub(title, author, body, language="en"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip")
        epub.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
        epub.writestr("OEBPS/content.opf", f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
    <dc:language>{language}</dc:language><dc:identifier>9780785217244</dc:identifier>
  </metadata>
  <manifest>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="cover"/><itemref idref="c1"/></spine>
</package>""")
        epub.writestr("OEBPS/cover.xhtml",
                      f"<html><body><h1>{title}</h1><p>{author}</p></body></html>")
        epub.writestr("OEBPS/text/ch1.xhtml", f"<html><body><p>{body}</p></body></html>")
    return buffer.getvalue()


def test_an_epub_gives_its_metadata_and_opening_text_in_reading_order():
    evidence = extract_evidence(make_epub("Romanov", "Nadine Brandes",
                                          "Chapter One. Ekaterinburg, 1918."), "epub")

    assert evidence.title == "Romanov"
    assert evidence.author == "Nadine Brandes"
    assert evidence.language == "en"
    assert "9780785217244" in evidence.identifiers
    assert evidence.text.index("Romanov") < evidence.text.index("Ekaterinburg")


def test_the_opening_text_is_capped():
    evidence = extract_evidence(make_epub("Long", "Writer", "word " * 20_000), "epub")

    assert len(evidence.text) <= 8_000


def test_an_fb2_gives_its_title_info_and_text():
    fb2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description><title-info>
    <author><first-name>M. L.</first-name><last-name>Wang</last-name></author>
    <book-title>The Sword of Kaigen</book-title><lang>en</lang>
  </title-info></description>
  <body><section><p>The Kotetsu house stood above the sea.</p></section></body>
</FictionBook>""".encode()

    evidence = extract_evidence(fb2, "fb2")

    assert evidence.title == "The Sword of Kaigen"
    assert evidence.author == "M. L. Wang"
    assert evidence.language == "en"
    assert "Kotetsu house" in evidence.text


def test_a_zipped_fb2_is_read_too():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("book.fb2", """<?xml version="1.0"?>
<FictionBook><description><title-info><book-title>Zipped</book-title>
</title-info></description><body><p>Inside.</p></body></FictionBook>""")

    evidence = extract_evidence(buffer.getvalue(), "fb2")

    assert evidence.title == "Zipped"


def make_pdf(text):
    """A one-page PDF with a real text layer, built by hand."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def test_a_pdf_gives_the_text_of_its_first_pages():
    evidence = extract_evidence(make_pdf("The Viceroys by Federico De Roberto"), "pdf")

    assert "The Viceroys by Federico De Roberto" in evidence.text


@pytest.mark.parametrize("data, ext", [
    (b"BOOKMOBI" + b"\x00" * 100, "mobi"),
    (b"BOOKMOBI" + b"\x00" * 100, "azw3"),
    (b"not a zip at all", "epub"),
    (b"%PDF-1.4 garbage", "pdf"),
])
def test_a_file_it_cannot_read_gives_no_evidence(data, ext):
    """The pre-download check is then the only check, as agreed for mobi/azw3."""
    assert extract_evidence(data, ext) is None
