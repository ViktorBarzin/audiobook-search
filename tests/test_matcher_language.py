"""The matcher in a language other than English.

A photo share fetches the edition in the language on the cover (Viktor,
2026-10-29), so a Bulgarian cover must match Bulgarian rows written in
Cyrillic. Until then the matcher folded every title to ASCII, which turned
"Под игото" into an empty string that could never match anything.
"""

import pytest

from backend.goodreads.matcher import (
    Candidate, ShelfItem, canonical_language, in_language, normalize_author,
    normalize_title, rank_candidates, select_candidate,
)


def item(title, author):
    return ShelfItem(book_id="", title=title, author=author, isbn=None, added_at=None)


def cand(title, author, language="Bulgarian", ext="epub", md5=None, size_bytes=400_000):
    return Candidate(md5=md5 or ("b" * 32), title=title, author=author, ext=ext,
                     language=language, size_bytes=size_bytes, source="libgen")


def test_cyrillic_titles_survive_normalising():
    assert normalize_title("Под игото") == "под игото"
    assert normalize_title("Под игото: Роман из живота на българите") == "под игото"
    assert normalize_author("Иван Вазов") == "иван вазов"


def test_accented_latin_still_folds_to_plain_letters():
    assert normalize_title("Café Society") == "cafe society"
    assert normalize_author("Gabriel García Márquez") == "gabriel garcia marquez"


@pytest.mark.parametrize("written", ["Bulgarian", "bulgarian", "bg", "bul", "български", "Bulgarian, English"])
def test_language_names_fold_across_forms(written):
    assert in_language(written, "bulgarian")


@pytest.mark.parametrize("written,canonical", [
    ("English", "english"), ("eng", "english"), ("Russian", "russian"),
    ("Deutsch", "german"), ("Klingon", "klingon"), ("", ""), (None, ""),
])
def test_canonical_language(written, canonical):
    assert canonical_language(written) == canonical


def test_a_bulgarian_cover_takes_the_bulgarian_row():
    english = cand("Under the Yoke", "Ivan Vazov", language="English", md5="e" * 32)
    bulgarian = cand("Под игото", "Иван Вазов", md5="b" * 32)

    match = select_candidate(item("Под игото", "Иван Вазов"), [english, bulgarian],
                             language="bulgarian")

    assert match.candidate is bulgarian
    assert match.reason == "title_author"


def test_only_other_languages_is_a_miss_named_for_the_language():
    russian = cand("Под игото", "Иван Вазов", language="Russian")

    match = select_candidate(item("Под игото", "Иван Вазов"), [russian], language="bulgarian")

    assert match.candidate is None
    assert match.reason == "no_edition_in_language"


def test_english_stays_the_default_and_keeps_its_reason():
    italian = cand("The Viceroys", "Federico De Roberto", language="Italian")

    match = select_candidate(item("The Viceroys", "Federico De Roberto"), [italian])

    assert match.reason == "no_english_edition"


def test_named_language_ranks_before_an_unknown_one():
    unknown = cand("Под игото", "Иван Вазов", language="", md5="u" * 32)
    named = cand("Под игото", "Иван Вазов", md5="n" * 32)

    ranked = rank_candidates(item("Под игото", "Иван Вазов"), [unknown, named], language="bulgarian")

    assert [c.md5 for c, _ in ranked] == ["n" * 32, "u" * 32]


@pytest.mark.parametrize("old,modern", [
    ("Подъ игото", "Под игото"),          # Bulgarian before 1945: a final ъ on every hard ending
    ("Иванъ Вазовъ", "Иван Вазов"),
    ("Война и миръ", "Война и мир"),        # Russian before 1918
    ("Отцы и дѣти", "Отцы и дети"),
    ("Мѫдрость", "Мъдрость"),
])
def test_pre_reform_cyrillic_folds_to_modern_spelling(old, modern):
    """A photo of an old edition reads in its old spelling; libgen mostly
    lists the book in the modern one (Под игото, 2026-10-29)."""
    assert normalize_title(old) == normalize_title(modern)


def test_an_old_spelling_cover_matches_modern_rows():
    match = select_candidate(item("Подъ игото", "Иванъ Вазовъ"),
                             [cand("Под игото b f 7621785", "Иван Вазов")], language="bulgarian")

    assert match.candidate is not None
