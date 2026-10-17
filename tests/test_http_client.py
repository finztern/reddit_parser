import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import http_client  # noqa: E402
from scraper.health import ExecutorHealth  # noqa: E402
from scraper.models import FetchResult  # noqa: E402


class Response:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code = status
        self.data = data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Curl:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class CurlHandle:
    def __init__(self, response):
        self.current = Curl(response)
        self.swaps = 0

    def request_headers(self):
        return {"Accept-Language": "en-US"}

    def swap(self):
        self.swaps += 1


class Cookie:
    key = "session"
    value = "cookie-value"


class AiohttpSession:
    cookie_jar = [Cookie()]


def run(coro):
    return asyncio.run(coro)


def fetch_page(response, *, after=None, health=None):
    curl = CurlHandle(response)
    pool = ThreadPoolExecutor(max_workers=1)
    executor = type("Executor", (), {"current": pool})()
    try:
        result = run(http_client._fetch_comments_page(
            AiohttpSession(), "https://reddit.example", "all+news", 25,
            curl, 10, "account", executor, after=after,
            connect_timeout=1, executor_health=health,
        ))
        return result, curl
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize(
    "status,headers,error_kind,retry_after",
    [
        (429, {"Retry-After": "12", "X-Ratelimit-Remaining": "1", "X-Ratelimit-Reset": "30"},
         "rate_or_server", 12.0),
        (401, {}, "auth", None),
        (403, {}, "auth", None),
        (503, {}, "rate_or_server", None),
        (404, {}, "rate_or_server", None),
    ],
)
def test_fetch_page_classifies_http_statuses(status, headers, error_kind, retry_after):
    result, _ = fetch_page(Response(status=status, headers=headers))

    assert result.status == status
    assert result.error_kind == error_kind
    assert result.retry_after == retry_after


def test_fetch_page_parses_comments_cursor_ratelimit_and_request_arguments():
    response = Response(
        data={"data": {"children": [
            {"kind": "t1", "data": {"name": "t1_comment"}},
            {"kind": "t3", "data": {"id": "post"}},
        ]}},
        headers={"X-Ratelimit-Remaining": "3.5", "X-Ratelimit-Reset": "60",
                 "X-Ratelimit-Used": "9"},
    )
    health = ExecutorHealth()

    result, curl = fetch_page(response, after="t1_previous", health=health)
    args, kwargs = curl.current.calls[0]

    assert result.comments == [{"name": "t1_comment"}]
    assert result.after == "t1_comment"
    assert result.ratelimit == {"remaining": 3.5, "reset": 60.0, "used": 9.0}
    assert args == ("https://reddit.example/r/all+news/comments.json",)
    assert kwargs["params"] == {"limit": 25, "after": "t1_previous"}
    assert kwargs["cookies"] == {"session": "cookie-value"}
    assert kwargs["headers"] == {"Accept-Language": "en-US"}
    assert kwargs["timeout"] == (1, 10)
    assert kwargs["allow_redirects"] is True
    assert health.active == 0


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "0"}, {"remaining": 0, "reset": 0, "used": None}),
        ({"x-ratelimit-remaining": "2", "x-ratelimit-reset": "3600", "x-ratelimit-used": "1"},
         {"remaining": 2, "reset": 3600, "used": 1}),
        ({"X-Ratelimit-Remaining": "-1", "X-Ratelimit-Reset": "5"}, None),
        ({"X-Ratelimit-Remaining": "1", "X-Ratelimit-Reset": "3601"}, None),
        ({"X-Ratelimit-Remaining": "bad", "X-Ratelimit-Reset": "5"}, None),
        ({}, None),
    ],
)
def test_parse_ratelimit_headers(headers, expected):
    assert http_client.parse_ratelimit_headers(Response(headers=headers)) == expected


@pytest.mark.parametrize("value,expected", [("2.5", 2.5), ("bad", None), (None, None)])
def test_parse_retry_after(value, expected):
    headers = {} if value is None else {"Retry-After": value}
    assert http_client.parse_retry_after(Response(headers=headers)) == expected


@pytest.mark.parametrize(
    "body,expected_kind",
    [("", "empty"), (" <html>blocked</html>", "empty"), ("not-json", "network")],
)
def test_fetch_page_classifies_invalid_json_responses(body, expected_kind):
    result, _ = fetch_page(Response(data=json.JSONDecodeError("invalid", body, 0), text=body))

    assert result.error_kind == expected_kind
    assert result.status == (200 if expected_kind == "empty" else None)


def test_fetch_page_classifies_request_exceptions_as_network_errors():
    result, _ = fetch_page(cffi_requests.RequestsError("connection failed"))
    assert result.error_kind == "network"


@pytest.mark.parametrize("children,after,expected_kind,expected_after", [
    ([], None, "empty", None),
    ([], "t1_previous", None, None),
    ([{"kind": "t1", "data": {"name": "t1_comment"}}], None, None, "t1_comment"),
])
def test_fetch_page_handles_empty_listing_and_cursor_fallback(
    children, after, expected_kind, expected_after
):
    result, _ = fetch_page(
        Response(data={"data": {"children": children, "dist": len(children)}}),
        after=after,
    )

    assert result.error_kind == expected_kind
    assert result.after == expected_after


def test_fetch_comments_stops_by_age_and_after_missing(monkeypatch):
    pages = [
        FetchResult(comments=[{"id": "recent", "created_utc": time.time()}],
                    after="t1_next", ratelimit={"remaining": 5}),
        FetchResult(comments=[{"id": "old", "created_utc": time.time() - 100}],
                    after="t1_old", ratelimit={"remaining": 4}),
    ]
    calls = []

    async def page(*args, **kwargs):
        calls.append(args[8])
        return pages.pop(0)

    monkeypatch.setattr(http_client, "_fetch_comments_page", page)
    result = run(http_client.fetch_comments(
        AiohttpSession(), "https://reddit.example", "all", 100,
        CurlHandle(None), 10, "account", object(), max_age=30, max_pages=0,
    ))

    assert calls == [None, "t1_next"]
    assert [comment["id"] for comment in result.comments] == ["recent", "old"]
    assert result.pages_fetched == 2
    assert result.ratelimit == {"remaining": 4}


def test_fetch_comments_stops_when_callback_matches(monkeypatch):
    calls = []

    async def page(*_args, **kwargs):
        calls.append(_args[8])
        return FetchResult(comments=[{"id": len(calls), "created_utc": time.time()}],
                           after=f"t1_{len(calls)}")

    monkeypatch.setattr(http_client, "_fetch_comments_page", page)
    result = run(http_client.fetch_comments(
        AiohttpSession(), "https://reddit.example", "all", 100,
        CurlHandle(None), 10, "account", object(), max_age=30, max_pages=10,
        stop_when=lambda comments: comments[0]["id"] == 1,
    ))

    assert calls == [None]
    assert result.pages_fetched == 1


def test_fetch_comments_discards_partial_results_after_page_error(monkeypatch):
    pages = [
        FetchResult(comments=[{"id": "not-claimed", "created_utc": time.time()}], after="t1_next"),
        FetchResult(error_kind="network", status=503),
    ]

    async def page(*_args, **_kwargs):
        return pages.pop(0)

    monkeypatch.setattr(http_client, "_fetch_comments_page", page)
    result = run(http_client.fetch_comments(
        AiohttpSession(), "https://reddit.example", "all", 100,
        CurlHandle(None), 10, "account", object(), max_age=30, max_pages=3,
    ))

    assert result.error_kind == "network"
    assert result.status == 503
    assert result.comments == []


def test_fetch_comments_obeys_page_limit_and_pagination_delay(monkeypatch):
    calls = []

    async def page(*args, **kwargs):
        calls.append(args[8])
        return FetchResult(comments=[{"created_utc": time.time()}],
                           after=f"t1_{len(calls)}")

    async def sleep(delay):
        assert 0.01 <= delay <= 0.02

    monkeypatch.setattr(http_client, "_fetch_comments_page", page)
    monkeypatch.setattr(http_client.asyncio, "sleep", sleep)
    result = run(http_client.fetch_comments(
        AiohttpSession(), "https://reddit.example", "all", 100,
        CurlHandle(None), 10, "account", object(), max_age=30, max_pages=2,
        pagination_delay_min=0.01, pagination_delay_max=0.02,
    ))

    assert calls == [None, "t1_1"]
    assert result.pages_fetched == 2
