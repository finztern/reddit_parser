import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.pipeline import _item_ok, build_payload, send_batch_to_store  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("parent_id,expected", [
    ("t1_parent", "t1_parent"),
    ("t3_post", "t3_post"),
    ("t5_invalid", ""),
    ("", ""),
])
def test_build_payload_maps_comment_metadata(parent_id, expected):
    result = build_payload({
        "id": "comment",
        "name": "t1_fullname",
        "created_utc": 1_700_000_000,
        "body": "hello",
        "author": "writer",
        "permalink": "/r/python/comments/post/comment",
        "parent_id": parent_id,
        "subreddit": "python",
        "score": 12,
    })

    assert result["content"] == "hello"
    assert result["external_id"] == "t1_fullname"
    assert result["created_at"] == "2023-11-14T22:13:20.000000Z"
    assert result["url"] == "https://www.reddit.com/r/python/comments/post/comment"
    assert result["author"] == result["username"] == "writer"
    assert result["external_parent_id"] == expected
    assert json.loads(result["summary"]) == {
        "score": "12",
        "upvote_ratio": "",
        "num_comments": "",
        "communityName": "r/python",
        "data_type": "comment",
    }
    assert result["_created_utc"] == 1_700_000_000
    assert result["_age_seconds"] >= 0


def test_build_payload_rejects_missing_id_or_timestamp_and_fills_optional_fields():
    assert build_payload({"created_utc": 1}) is None
    assert build_payload({"id": "comment"}) is None

    result = build_payload({"id": "comment", "created_utc": 0})

    assert result["external_id"] == "t1_comment"
    assert result["content"] == ""
    assert result["author"] == ""
    assert result["url"] == ""
    assert result["external_parent_id"] == ""
    assert json.loads(result["summary"])["communityName"] == ""


@pytest.mark.parametrize("entry,expected", [
    (True, True),
    (False, False),
    ({"ok": 0}, False),
    ({"success": "yes"}, True),
    ({"stored": False}, False),
    ({"saved": True}, True),
    ({"error": "rejected"}, False),
    ({"other": "field"}, True),
    ("unknown", True),
])
def test_item_confirmation_formats(entry, expected):
    assert _item_ok(entry) is expected


class Response:
    def __init__(self, status=200, data=None, error=None, text="response"):
        self.status = status
        self.data = data
        self.error = error
        self.text_value = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def json(self, **_):
        if self.error:
            raise self.error
        return self.data

    async def text(self):
        return self.text_value


class Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, endpoint, *, json, timeout):
        self.calls.append((endpoint, json, timeout.total))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize("data,expected", [
    ({"results": [True, {"success": False}]}, [True, False]),
    ({"received": 1}, [True, True]),
    ({"received": 1, "results": []}, [True, True]),
    ([False, False], [True, True]),
])
def test_send_batch_accepts_confirmations_and_2xx_without_item_results(data, expected):
    session = Session(Response(data=data))
    items = [{"id": "one", "_age_seconds": 1}, {"id": "two", "_created_utc": 2}]

    assert run(send_batch_to_store(session, "endpoint", items, "acct", 10)) == expected
    assert session.calls[0][1] == [{"id": "one"}, {"id": "two"}]
    assert session.calls[0][2] == 15


@pytest.mark.parametrize("error", [
    ValueError("invalid JSON"),
    json.JSONDecodeError("invalid", "", 0),
])
def test_send_batch_marks_unparseable_success_response_unconfirmed(error):
    session = Session(Response(error=error))
    assert run(send_batch_to_store(session, "endpoint", [{"id": "one"}], "acct", 10)) == [False]


@pytest.mark.parametrize("error", [
    aiohttp.ClientError("connection lost"),
    asyncio.TimeoutError(),
])
def test_send_batch_marks_transport_errors_unconfirmed(error):
    session = Session(error=error)
    assert run(send_batch_to_store(session, "endpoint", [{"id": "one"}], "acct", 10)) == [False]


def test_send_batch_with_empty_input_does_not_make_request():
    session = Session()
    assert run(send_batch_to_store(session, "endpoint", [], "acct", 10)) == []
    assert session.calls == []
