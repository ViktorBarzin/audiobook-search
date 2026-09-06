"""The author on a real Anna's Archive page is a link under the title.

Measured live 2026-09-06, running the shortcut on the phone against the real
page. The server got the whole 263 KB, recovered the md5 and parsed the title,
and then:

    Parsed the posted page for 5b6e6e...: 'Obviously Awesome' by None
    No confident libgen match for 'Obviously Awesome' by 'Unknown Author'

Without an author the libgen title fallback cannot match anything, and this
book only exists on libgen under a different md5, so the whole download turns
on that one field.

The shape of the page, read off the phone's accessibility tree:

    <div>  <StaticText>Obviously Awesome</>  <Link>🔍</Link>  </div>
    <Link> April Dunford </Link>
    <Link> 2019 </Link>

The author is the first link after the title, and the year is the next one.
Every existing fallback looked for markup that page does not have: no
div.italic, no "by X" in the <title>, no og:description, no "Author:" label.
"""

import pytest

from backend.annas import AnnasArchiveScraper

MD5 = "5b6e6e722084ab2d8fdef68a30fe132b"

# Shaped like the live page: a file-path breadcrumb of links ABOVE the title,
# then the title, then the author link, then the year link.
REAL_SHAPE = """<!DOCTYPE html><html><head>
  <title>Obviously Awesome - Anna’s Archive</title>
</head><body>
  <div class="text-xs">
    <a href="/search?q=zlib">zlib/</a>
    <a href="/search?q=business">Business &amp; Economics/</a>
    <a href="/search?q=others">Others/</a>
    <a href="/search?q=dunford">April Dunford/</a>
    <a href="/md5/5b6e6e722084ab2d8fdef68a30fe132b">Obviously Awesome_28354015.epub</a>
  </div>
  <div class="text-3xl font-bold">Obviously Awesome</div>
  <a href="/search?q=Obviously+Awesome">\U0001f50d</a>
  <div><a href="/search?q=April+Dunford">&nbsp;April Dunford</a></div>
  <div><a href="/search?q=2019">&nbsp;2019</a></div>
  <div>English [en] · EPUB · 1.8MB · 2019</div>
  <a href="/slow_download/5b6e6e722084ab2d8fdef68a30fe132b/0/0">Slow download</a>
</body></html>"""


@pytest.fixture
def scraper():
    return AnnasArchiveScraper()


def test_the_author_link_under_the_title_is_the_author(scraper):
    detail = scraper.parse_detail(REAL_SHAPE, MD5)

    assert detail is not None
    assert detail.title == "Obviously Awesome"
    assert detail.author == "April Dunford"


def test_the_breadcrumb_above_the_title_is_not_the_author(scraper):
    """"April Dunford/" is a path segment, and it comes first in the document."""
    detail = scraper.parse_detail(REAL_SHAPE, MD5)

    assert not detail.author.endswith("/")


def test_a_year_link_is_not_mistaken_for_an_author(scraper):
    no_author = REAL_SHAPE.replace(
        '<div><a href="/search?q=April+Dunford">&nbsp;April Dunford</a></div>', ""
    )

    detail = scraper.parse_detail(no_author, MD5)

    assert detail.author in (None, ""), f"got {detail.author!r} from a year link"


def test_the_older_italic_markup_still_wins(scraper):
    """Pages that do carry div.italic must keep parsing the way they did."""
    italic = REAL_SHAPE.replace(
        '<div class="text-3xl font-bold">',
        '<div class="italic">Somebody Else</div><div class="text-3xl font-bold">',
    )

    detail = scraper.parse_detail(italic, MD5)

    assert detail.author == "Somebody Else"


def test_a_page_with_no_author_anywhere_says_so_in_the_log(scraper, caplog):
    """The next time the markup moves, one log line should show what arrived."""
    import logging

    caplog.set_level(logging.INFO)
    bare = """<html><head><title>Some Book - Anna’s Archive</title></head>
    <body><div class="text-3xl">Some Book</div></body></html>"""

    scraper.parse_detail(bare, MD5)

    logged = " ".join(rec.message for rec in caplog.records)
    assert "Some Book" in logged, "the excerpt around the title should be logged"
