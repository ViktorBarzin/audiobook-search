"""The detail-mirror and md5-lookup routes report a refused upload as a failure.

Both routes returned True whenever a file downloaded, whatever Calibre-Web made
of it, so a book Calibre turned away still read as "Added to Calibre". The
libgen hash route stopped doing that on 2026-09-24; these two now match it.
"""

import backend.main as bs_main
from backend.models import AudiobookDetail

MD5 = "b952ccc999dba94693da7100fe705923"
FILE = b"%PDF-1.4" + b"x" * 50_000


class NoLibgen:
    async def download_file(self, md5):
        return None, None

    async def search_candidates(self, query):
        return []


class Mirrors:
    def __init__(self, direct=True, md5_lookup=True):
        self.direct, self.md5_lookup = direct, md5_lookup

    async def download_file(self, url):
        return (FILE, "Moby-Dick.pdf") if self.direct else (None, None)

    async def download_from_libgen_md5(self, md5):
        return (FILE, "Moby-Dick.pdf") if self.md5_lookup else (None, None)


def detail():
    return AudiobookDetail(
        id=f"annas:{MD5}", title="Moby-Dick", author="Herman Melville",
        url=f"https://annas-archive.org/md5/{MD5}", source="annas",
        content_type="ebook", magnet_url="",
        mirror_urls=["https://libgen.li/ads.php?md5=" + MD5],
    )


async def refused(data, filename):
    return None


async def test_a_mirror_file_calibre_refuses_is_not_a_success(monkeypatch):
    monkeypatch.setattr(bs_main, "libgen_scraper", NoLibgen())
    monkeypatch.setattr(bs_main, "annas_scraper", Mirrors(direct=True, md5_lookup=False))
    monkeypatch.setattr(bs_main, "_upload_to_calibre", refused)

    job = {}
    ok = await bs_main._try_direct_download("j", job, MD5, "Moby-Dick", "Herman Melville", detail())

    assert ok is False
    assert "did not accept" in job["upload_refused"]


async def test_an_md5_lookup_file_calibre_refuses_is_not_a_success(monkeypatch):
    monkeypatch.setattr(bs_main, "libgen_scraper", NoLibgen())
    monkeypatch.setattr(bs_main, "annas_scraper", Mirrors(direct=False, md5_lookup=True))
    monkeypatch.setattr(bs_main, "_upload_to_calibre", refused)

    job = {}
    ok = await bs_main._try_direct_download("j", job, MD5, "Moby-Dick", "Herman Melville", detail())

    assert ok is False
    assert "did not accept" in job["upload_refused"]
