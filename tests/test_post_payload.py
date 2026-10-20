import json

from scraper.pipeline import build_post_payload


def test_post_payload_selftext_and_summary():
    p = build_post_payload({"id": "abc", "name": "t3_abc", "created_utc": 1_700_000_000, "title": "T",
                            "selftext": "body", "author": "u", "permalink": "/r/x/comments/abc/t/",
                            "subreddit": "x", "score": 5, "upvote_ratio": 0.9, "num_comments": 3})
    assert p["external_id"] == "t3_abc" and p["content"] == "body" and p["title"] == "T"
    assert p["external_parent_id"] == "" and p["url"].endswith("/r/x/comments/abc/t/")
    s = json.loads(p["summary"])
    assert s["data_type"] == "post" and s["num_comments"] == "3" and s["upvote_ratio"] == "0.9"
    assert s["communityName"] == "r/x"


def test_post_payload_falls_back_to_title_and_skips_invalid():
    p = build_post_payload({"id": "a", "created_utc": 1.0, "title": "Only title", "selftext": "[removed]"})
    assert p["content"] == "Only title" and p["external_id"] == "t3_a"
    assert build_post_payload({"created_utc": 1.0}) is None
    assert build_post_payload({"id": "a"}) is None


def test_is_nsfw():
    from scraper.pipeline import is_nsfw
    assert is_nsfw({"over_18": True})
    assert is_nsfw({"quarantine": True})
    assert not is_nsfw({"over_18": False, "subreddit": "aww"})
    assert is_nsfw({"subreddit": "GoneWild"}, frozenset({"gonewild"}))
    assert not is_nsfw({"subreddit": "aww"}, frozenset({"gonewild"}))
