"""What the share-a-link endpoint accepts.

Viktor shares books from his phone with a shortcut. He might be on Anna's
Archive, on libgen, or just holding a hash — the endpoint should take any of
them rather than rejecting the ones it did not expect.
"""

import pytest

from backend.main import extract_md5

MD5 = "a20d0fd467994da99bfca76dc899cee7"


@pytest.mark.parametrize("shared", [
    f"https://annas-archive.pk/md5/{MD5}",
    f"https://annas-archive.gl/md5/{MD5}?q=whatever",
    f"HTTPS://ANNAS-ARCHIVE.PK/MD5/{MD5.upper()}",
    f"https://libgen.li/ads.php?md5={MD5}",
    f"https://libgen.li/get.php?md5={MD5}&key=ABC123",
    f"https://libgen.vg/index.php?md5={MD5}",
    MD5,
    f"  {MD5}  ",
    f"Check out this book {MD5} from AA",
])
def test_accepts_every_shape_a_share_might_take(shared):
    assert extract_md5(shared) == MD5


@pytest.mark.parametrize("junk", [
    "",
    "https://annas-archive.pk/search?q=neuromancer",
    "https://example.com/nothing-here",
    "deadbeef",                      # too short to be an md5
    "z" * 32,                        # right length, not hex
])
def test_rejects_things_with_no_book_in_them(junk):
    assert extract_md5(junk) is None


def test_a_truncated_hash_is_not_accepted():
    """Padding a short hash out to look valid must not sneak through."""
    assert extract_md5("2c384414cb") is None
