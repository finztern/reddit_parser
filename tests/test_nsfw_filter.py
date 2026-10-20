import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.utils import is_nsfw  # noqa: E402


def test_is_nsfw_flags():
    assert is_nsfw({"over_18": True})
    assert is_nsfw({"link_over_18": True})
    assert is_nsfw({"subreddit_over18": "true"})
    assert not is_nsfw({"over_18": False})
    assert not is_nsfw({"over_18": "false"})
    assert not is_nsfw({})
