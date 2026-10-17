import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import worker  # noqa: E402


def test_fullname_falls_back_to_comment_id():
    assert worker._fullname({"name": "t1_fullname", "id": "comment"}) == "t1_fullname"
    assert worker._fullname({"id": "comment"}) == "t1_comment"
    assert worker._fullname({}) == "t1_None"


@pytest.mark.parametrize(
    "ratelimit,margin,max_interval,expected",
    [
        (None, 1, 100, 0),
        ({"remaining": 1, "reset": 30}, 2, 100, 31),
        ({"remaining": 5, "reset": 20}, 2, 100, 2),
        ({"remaining": 5, "reset": 0}, 2, 100, 0),
        ({"remaining": 0, "reset": 100}, 1, 10, 10),
    ],
)
def test_rate_limit_cooldown_calculation(ratelimit, margin, max_interval, expected):
    assert worker.rl_cooldown(ratelimit, margin, max_interval) == expected


def test_supervised_retries_with_exponential_capped_delay(monkeypatch):
    async def scenario():
        attempts = []
        delays = []

        async def unreliable(value):
            attempts.append(value)
            if len(attempts) < 4:
                raise RuntimeError("temporary failure")

        async def sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(worker.asyncio, "sleep", sleep)
        await worker.supervised(
            unreliable, "account", name="acct",
            base_backoff=2, max_backoff=5,
        )

        assert attempts == ["account"] * 4
        assert delays == [2, 4, 5]

    asyncio.run(scenario())


def test_supervised_returns_when_worker_finishes_normally():
    async def scenario():
        calls = []

        async def finished():
            calls.append(True)

        await worker.supervised(finished)
        assert calls == [True]

    asyncio.run(scenario())


def test_supervised_propagates_cancellation_without_retry(monkeypatch):
    async def scenario():
        calls = []

        async def cancelled():
            calls.append(True)
            raise asyncio.CancelledError

        async def unexpected_sleep(_delay):
            raise AssertionError("cancellation must not retry")

        monkeypatch.setattr(worker.asyncio, "sleep", unexpected_sleep)
        with pytest.raises(asyncio.CancelledError):
            await worker.supervised(cancelled)
        assert calls == [True]

    asyncio.run(scenario())


def test_account_worker_waits_for_missing_cookie_file_and_is_cancellable(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "BASE_DIR", tmp_path)

    async def cancel_wait(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", cancel_wait)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.account_worker(
            {"name": "acct", "cookie_file": "guest.json", "proxy_port": 1},
            object(), object(), object(), object(), object(),
        ))


def test_account_worker_retries_unreadable_cookie_file(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "BASE_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("{invalid", encoding="utf-8")

    class Config:
        def get(self, _key, default=None):
            return default

    async def cancel_wait(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", cancel_wait)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.account_worker(
            {"name": "acct", "cookie_file": "bad.json", "proxy_port": 1},
            Config(), object(), object(), object(), object(),
        ))
