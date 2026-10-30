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


# ---- требование заказчика: ВСЕ ссылки обязаны попасть в content ----

def _post(**kw):
    base = {"id": "p1", "name": "t3_p1", "created_utc": 1_700_000_000, "title": "Nice", "selftext": "",
            "author": "u", "permalink": "/r/pics/comments/p1/nice/", "subreddit": "pics", "is_self": False}
    return build_post_payload({**base, **kw})


def test_image_post_content_includes_target_link():
    p = _post(url="https://i.redd.it/abc.jpg", url_overridden_by_dest="https://i.redd.it/abc.jpg")
    assert p["content"] == "Nice\n\nhttps://i.redd.it/abc.jpg"


def test_link_post_unescapes_html_entities_in_url():
    p = _post(url_overridden_by_dest="https://ex.com/a?x=1&amp;y=2", title="A &amp; B")
    assert p["content"] == "A & B\n\nhttps://ex.com/a?x=1&y=2" and p["title"] == "A & B"


def test_self_post_keeps_selftext_links_without_duplicates():
    text = "see [this](https://ex.com/p?a=1&amp;b=2) and https://other.org/x"
    p = _post(is_self=True, selftext=text, url="https://www.reddit.com/r/pics/comments/p1/nice/")
    assert p["content"] == "see [this](https://ex.com/p?a=1&b=2) and https://other.org/x"   # permalink не дублируется


def test_selftext_with_target_link_not_duplicated():
    p = _post(selftext="look https://ex.com/z", url="https://ex.com/z")
    assert p["content"] == "look https://ex.com/z"


def test_gallery_video_and_crosspost_links():
    meta = {"a1": {"status": "valid", "e": "Image", "m": "image/jpg", "s": {"u": "https://preview.redd.it/a1.jpg?s=x"}},
            "b2": {"status": "valid", "e": "AnimatedImage", "m": "image/gif", "s": {"gif": "g"}},
            "c3": {"status": "failed", "e": "Image", "m": "image/png"},
            "e4": {"status": "valid", "e": "Image", "m": "image/jpeg"}}
    p = _post(url="https://www.reddit.com/gallery/p1", gallery_data={"items": [{"media_id": "b2"}, {"media_id": "a1"}]},
              media_metadata=meta,
              secure_media={"reddit_video": {"fallback_url": "https://v.redd.it/xyz/DASH_720.mp4?source=fallback"}},
              crosspost_parent_list=[{"url": "https://www.reddit.com/r/orig/comments/o1/t/"}])
    lines = p["content"].split("\n\n", 1)[1].split("\n")
    assert lines == ["https://www.reddit.com/gallery/p1", "https://i.redd.it/b2.gif", "https://i.redd.it/a1.jpg",
                     "https://i.redd.it/e4.jpg", "https://v.redd.it/xyz/DASH_720.mp4?source=fallback",
                     "https://www.reddit.com/r/orig/comments/o1/t/"]                  # failed-запись пропущена


def test_comment_content_unescaped_with_media_and_profile_url():
    from scraper.pipeline import build_payload
    c = build_payload({"id": "c1", "name": "t1_c1", "created_utc": 1_700_000_000, "author": "H0tpR0bL3m23",
                       "body": "&gt; quote https://ex.com/?a=1&amp;b=2 ![img](m1)", "subreddit": "photography",
                       "permalink": "/r/photography/comments/1uhjuvy/best_angle/c1/", "score": 1,
                       "media_metadata": {"m1": {"status": "valid", "e": "Image", "m": "image/png"}}})
    assert c["content"] == "> quote https://ex.com/?a=1&b=2 ![img](m1)\n\nhttps://i.redd.it/m1.png"
    assert c["userprofile_url"] == "https://www.reddit.com/user/H0tpR0bL3m23"
    gone = build_payload({"id": "c2", "created_utc": 1, "author": "[deleted]", "body": "x"})
    assert gone["userprofile_url"] == ""


def test_summary_is_compact_like_the_schema_sample():
    from scraper.pipeline import build_payload
    c = build_payload({"id": "ou8gehn", "name": "t1_ou8gehn", "created_utc": 1_782_610_508, "author": "a",
                       "subreddit": "photography", "score": 1, "body": "x", "parent_id": "t3_1uhjuvy"})
    assert c["summary"] == ('{"score":"1","upvote_ratio":"","num_comments":"","communityName":"r/photography",'
                            '"data_type":"comment"}')


def test_reddit_preview_links_are_appended_without_duplicates():
    prev = {"images": [{"source": {"url": "https://external-preview.redd.it/a.jpg?auto=webp&amp;s=sig", "width": 1},
                        "resolutions": [{"url": "https://external-preview.redd.it/a.jpg?width=108&amp;s=small"}],
                        "variants": {"gif": {"source": {"url": "https://preview.redd.it/a.gif?s=g"}},
                                     "mp4": {"source": {"url": "https://preview.redd.it/a.mp4?s=m"}}}}],
            "enabled": True}
    p = _post(url_overridden_by_dest="https://news.example.com/story", preview=prev)
    lines = p["content"].split("\n\n", 1)[1].split("\n")
    assert lines == ["https://news.example.com/story", "https://external-preview.redd.it/a.jpg?auto=webp&s=sig",
                     "https://preview.redd.it/a.gif?s=g", "https://preview.redd.it/a.mp4?s=m"]
    # ссылка уже есть в тексте поста — второй раз не добавляется
    p2 = _post(is_self=True, selftext="see https://preview.redd.it/a.gif?s=g", preview=prev)
    assert p2["content"].count("https://preview.redd.it/a.gif?s=g") == 1
