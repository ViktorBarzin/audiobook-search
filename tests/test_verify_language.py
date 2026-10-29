"""The Claude check for a book wanted in a language other than English.

A photo share wants the edition in the cover's language. The check used to
refuse "any text that is not in English", which would refuse every Bulgarian
file the matcher had correctly chosen.
"""

import json

import httpx

from backend.goodreads.verify import (
    SYSTEM_PROMPT, ClaudeVerifier, FileEvidence, OfferedFile, WantedBook,
    file_prompt, record_prompt, system_prompt,
)

BULGARIAN = WantedBook(title="Под игото", author="Иван Вазов", language="bulgarian", source="photo")
OFFERED = OfferedFile(title="Под игото", author="Иван Вазов", language="Bulgarian", ext="epub")


def test_english_keeps_the_prompt_it_had():
    assert system_prompt(None) == SYSTEM_PROMPT
    assert system_prompt("english") == SYSTEM_PROMPT


def test_another_language_replaces_english_in_the_rules():
    prompt = system_prompt("bulgarian")

    assert "not in Bulgarian" in prompt
    assert "translation into Bulgarian" in prompt
    assert "English" not in prompt


def test_a_photo_share_says_where_the_wanted_book_came_from():
    prompt = record_prompt(BULGARIAN, OFFERED)

    assert "photo of the book's cover" in prompt
    assert "Goodreads" not in prompt
    assert "Language: Bulgarian" in prompt


def test_the_file_question_names_the_language():
    evidence = FileEvidence(format="epub", title="Под игото", text="Глава първа")

    assert file_prompt(BULGARIAN, OFFERED, evidence).endswith("is it the wanted book, in Bulgarian?")


async def test_the_verifier_sends_the_language_prompt():
    sent = {}

    def answer(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        reply = '{"verdict": "yes", "reason": "same novel"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    verifier = ClaudeVerifier("http://agent", "token", transport=httpx.MockTransport(answer))
    verdict = await verifier.check_record(BULGARIAN, OFFERED)

    assert verdict.ok
    assert sent["messages"][0]["content"] == system_prompt("bulgarian")
