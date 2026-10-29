"""Turning a photo of a cover into a cover reading.

iPhone photos arrive as HEIC, which claude-agent-service does not decode, so
every photo is turned into a JPEG of at most PHOTO_MAX_EDGE pixels first. Claude
then reads the cover with no tools at all: the text on a photo is written by
strangers, and an agent with tools reading it would be an injection path.
"""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from backend.photo import (
    PHOTO_MAX_EDGE, CoverReader, CoverReaderUnavailable, PhotoUnreadable,
    normalise_photo, parse_reading,
)


def picture(fmt: str, size=(3024, 4032), exif_rotate: bool = False) -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    out = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 clockwise to display
        image.save(out, fmt, exif=exif)
    else:
        image.save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_any_phone_format_becomes_a_small_jpeg(fmt):
    if fmt == "HEIF":
        import pillow_heif
        pillow_heif.register_heif_opener()

    jpeg = normalise_photo(picture(fmt))

    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    assert max(image.size) == PHOTO_MAX_EDGE
    assert image.size == (1176, 1568)


def test_the_camera_orientation_is_applied():
    jpeg = normalise_photo(picture("JPEG", size=(4032, 3024), exif_rotate=True))

    assert Image.open(io.BytesIO(jpeg)).size == (1176, 1568)


def test_a_small_photo_is_not_enlarged():
    jpeg = normalise_photo(picture("PNG", size=(600, 800)))

    assert Image.open(io.BytesIO(jpeg)).size == (600, 800)


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"%PDF-1.4 something"])
def test_something_that_is_not_a_picture_is_refused(data):
    with pytest.raises(PhotoUnreadable):
        normalise_photo(data)


def test_a_readable_cover():
    reading = parse_reading(json.dumps({
        "readable": True, "title": " Под игото ", "author": "Иван Вазов",
        "language": "bg", "isbn": "978-954-07-1234-5", "note": "clear front cover",
    }))

    assert reading.readable
    assert reading.title == "Под игото"
    assert reading.author == "Иван Вазов"
    assert reading.language == "bulgarian"
    assert reading.isbn == "9789540712345"


def test_an_unreadable_photo_keeps_what_claude_saw():
    reading = parse_reading('{"readable": false, "note": "a blurry photo of a shelf"}')

    assert not reading.readable
    assert reading.note == "a blurry photo of a shelf"


def test_a_cover_with_no_title_is_not_readable():
    reading = parse_reading('{"readable": true, "title": "", "author": "Someone"}')

    assert not reading.readable


def test_the_language_defaults_to_english():
    reading = parse_reading('{"readable": true, "title": "Dune", "author": "Frank Herbert"}')

    assert reading.language == "english"


@pytest.mark.parametrize("text", ["", "It is Dune.", '{"title": "Dune"}', "[1, 2]"])
def test_an_answer_that_cannot_be_read_is_an_outage(text):
    with pytest.raises(CoverReaderUnavailable):
        parse_reading(text)


async def test_the_reader_sends_the_photo_with_no_tools():
    sent = {}

    def answer(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["auth"] = request.headers["authorization"]
        sent["body"] = json.loads(request.content)
        reply = '{"readable": true, "title": "Dune", "author": "Frank Herbert", "language": "English"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    reader = CoverReader("http://agent/", "token", transport=httpx.MockTransport(answer))
    reading = await reader.read(b"\xff\xd8jpeg-bytes")

    assert reading.title == "Dune"
    assert sent["url"] == "http://agent/v1/chat/completions"
    assert sent["auth"] == "Bearer token"
    body = sent["body"]
    assert body["no_tools"] is True
    assert body["model"] == "sonnet"
    parts = body["messages"][-1]["content"]
    image = next(p for p in parts if p["type"] == "image_url")
    assert image["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg-bytes").decode()


@pytest.mark.parametrize("response", [
    httpx.Response(503, json={"error": "execution failed"}),
    httpx.Response(200, json={"unexpected": True}),
])
async def test_an_agent_service_problem_is_an_outage(response):
    reader = CoverReader("http://agent", "token", transport=httpx.MockTransport(lambda r: response))

    with pytest.raises(CoverReaderUnavailable):
        await reader.read(b"jpeg")


async def test_an_unconfigured_reader_is_an_outage():
    with pytest.raises(CoverReaderUnavailable):
        await CoverReader("", "").read(b"jpeg")
