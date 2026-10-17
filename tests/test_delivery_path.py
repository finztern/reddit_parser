import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as app  # noqa: E402
from scraper import pipeline  # noqa: E402
from scraper.sender import DEMAND_NONE, DEMAND_NORMAL, DEMAND_URGENT, Sender  # noqa: E402
from scraper.state import SeenCache  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def payload(item_id, created_utc=None):
    return {
        "external_id": item_id,
        "_created_utc": time.time() if created_utc is None else created_utc,
        "content": item_id,
    }


def sender_config(**queue_control):
    return {
        "store_endpoint": "https://collector.example/store_items",
        "max_age_seconds": 30,
        "batch_max_items": 10,
        "queue_control": {
            "enabled": True,
            "url": "https://collector.example/queue",
            "inflight_target": 2,
            "send_batch_size": 2,
            "queue_target": 4,
            "buffer_target_factor": 2,
            "buffer_max_factor": 3,
            "min_send_gap_seconds": 0,
            "flush_age_fraction": 0.5,
            "low_queue_fraction": 0.5,
            "respect_server_action": True,
            **queue_control,
        },
    }


async def claimed_seen(*ids):
    seen = SeenCache(20)
    for item_id in ids:
        assert await seen.try_claim(item_id)
    return seen


def test_send_batch_to_store_chunks_payload_and_tracks_item_results():
    class Response:
        status = 200

        def __init__(self, result):
            self.result = result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def json(self, **_):
            return self.result

    class Session:
        def __init__(self):
            self.bodies = []

        def post(self, _endpoint, *, json, timeout):
            self.bodies.append(json)
            assert timeout.total == 15
            result = {"results": [{"ok": True}]} if len(self.bodies) == 1 else {
                "results": [{"error": "rejected"}]
            }
            return Response(result)

    session = Session()
    items = [
        {"external_id": "one", "content": "first", "_created_utc": 1},
        {"external_id": "two", "content": "second", "_created_utc": 2},
    ]

    flags = run(pipeline.send_batch_to_store(
        session, "https://collector.example/store_items", items, "test", batch_max_items=1
    ))

    assert flags == [True, False]
    assert session.bodies == [[{"external_id": "one", "content": "first"}],
                              [{"external_id": "two", "content": "second"}]]


@pytest.mark.parametrize("status", [413, 503])
def test_send_batch_to_store_releases_unconfirmed_http_chunks(status):
    class Response:
        def __init__(self):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def text(self):
            return "failed"

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    flags = run(pipeline.send_batch_to_store(
        Session(), "https://collector.example/store_items", [{"id": 1}], "test", 10
    ))
    assert flags == [False]


def test_sender_confirms_success_and_keeps_failed_item_claimed_for_retry(monkeypatch):
    seen = run(claimed_seen("t1_ok", "t1_retry"))
    sender = Sender(sender_config(), seen, object(), logging.getLogger("test"))
    items = [payload("t1_ok"), payload("t1_retry")]

    async def send(_session, _endpoint, _items, _account, _limit):
        return [True, False]

    monkeypatch.setattr("scraper.sender.send_batch_to_store", send)
    assert run(sender._send(items)) == 1
    assert seen.contains("t1_ok")
    assert "t1_ok" not in seen._pending
    assert seen.contains("t1_retry")
    assert "t1_retry" in seen._pending
    assert sender._buf == [items[1]]

    async def retry_success(_session, _endpoint, _items, _account, _limit):
        return [True]

    monkeypatch.setattr("scraper.sender.send_batch_to_store", retry_success)
    assert run(sender._send(sender._buf)) == 1
    assert seen.contains("t1_retry")
    assert "t1_retry" not in seen._pending


def test_sender_releases_items_when_store_call_raises(monkeypatch):
    seen = run(claimed_seen("t1_a", "t1_b"))
    sender = Sender(sender_config(), seen, object(), logging.getLogger("test"))

    async def fail(*_args):
        raise aiohttp.ClientError("connection lost")

    monkeypatch.setattr("scraper.sender.send_batch_to_store", fail)
    with pytest.raises(aiohttp.ClientError, match="connection lost"):
        run(sender._send([payload("t1_a"), payload("t1_b")]))

    assert not seen.contains("t1_a")
    assert not seen.contains("t1_b")


def test_sender_demand_tracks_fill_and_starvation():
    async def scenario():
        seen = await claimed_seen("one", "two", "three", "four")
        sender = Sender(sender_config(), seen, object(), logging.getLogger("test"))
        assert sender.demand() == DEMAND_NORMAL
        await sender.put_many([payload("one"), payload("two"), payload("three"), payload("four")])
        assert sender.demand() == DEMAND_NONE
        sender._starving = True
        del sender._buf[1:]
        assert sender.demand() == DEMAND_URGENT

    run(scenario())


@pytest.mark.parametrize(
    "queue_status",
    [
        {"bpipe_queue": 4, "bpipe_max": 20},
        {"bpipe_queue": 0, "bpipe_max": 20, "degraded": True},
        {"bpipe_queue": 0, "bpipe_max": 20, "action": "pause"},
    ],
)
def test_sender_respects_queue_capacity_degraded_and_server_pause(monkeypatch, queue_status):
    async def scenario():
        seen = await claimed_seen("one", "two")
        sender = Sender(sender_config(), seen, object(), logging.getLogger("test"))
        await sender.put_many([payload("one"), payload("two")])
        flushes = []

        async def fetch(*_args):
            return queue_status

        async def flush(count, partial):
            flushes.append((count, partial))

        monkeypatch.setattr(sender, "_fetch", fetch)
        monkeypatch.setattr(sender, "_flush", flush)
        await sender._tick()

        assert flushes == []
        assert sender.backlog == 2
        assert sender.demand() == DEMAND_NORMAL

    run(scenario())


def test_sender_flushes_batch_when_queue_is_below_target(monkeypatch):
    async def scenario():
        seen = await claimed_seen("one", "two")
        sender = Sender(sender_config(), seen, object(), logging.getLogger("test"))
        await sender.put_many([payload("one"), payload("two")])
        flushes = []

        async def fetch(*_args):
            return {"bpipe_queue": 1, "bpipe_max": 20}

        async def flush(count, partial):
            flushes.append((count, partial))

        monkeypatch.setattr(sender, "_fetch", fetch)
        monkeypatch.setattr(sender, "_flush", flush)
        await sender._tick()

        assert flushes == [(2, False)]

    run(scenario())


def test_sender_does_not_make_decisions_on_transient_queue_failure(monkeypatch):
    async def scenario():
        seen = await claimed_seen("one", "two")
        sender = Sender(sender_config(), seen, object(), logging.getLogger("test"))
        await sender.put_many([payload("one"), payload("two")])
        sender._status = {"bpipe_queue": 0, "bpipe_max": 20}
        flushes = []

        async def fetch(*_args):
            return None

        async def flush(*args):
            flushes.append(args)

        monkeypatch.setattr(sender, "_fetch", fetch)
        monkeypatch.setattr(sender, "_flush", flush)
        await sender._tick()

        assert flushes == []
        assert sender.backlog == 2

    run(scenario())


def test_main_gracefully_cancels_tasks_and_shuts_down_executor(monkeypatch):
    async def scenario():
        stopped = []
        cleanup = []
        config_values = {
            "metrics": {"enabled": False},
            "queue_control": {"enabled": False},
            "stream_subs": ["all"],
            "target_rate_per_second": 80,
            "max_age_seconds": 15,
            "stream_start_interval_seconds": 1.5,
        }

        class Config:
            def __init__(self, *_args):
                pass

            def load_once(self):
                pass

            def get(self, key, default=None):
                return config_values.get(key, default)

            async def reload_loop(self):
                await asyncio.Event().wait()

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Executor:
            def shutdown(self, **kwargs):
                stopped.append(kwargs)

        class SenderStub:
            def __init__(self, *_args, **_kwargs):
                pass

            def demand(self):
                return DEMAND_NORMAL

            async def run(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.append("sender")

        class SchedulerStub:
            def __init__(self, *_args, **_kwargs):
                pass

            async def run(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.append("scheduler")

            async def stats_loop(self, *_args, **_kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.append("stats")

        async def health_loop(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.append("health")

        async def worker(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.append("worker")

        def install_signal_handler(_loop, stop_event):
            _loop.call_soon(stop_event.set)

        executor = Executor()
        monkeypatch.setattr(app, "ConfigStore", Config)
        monkeypatch.setattr(app, "load_accounts", lambda: [{"name": "acct", "enabled": True}])
        monkeypatch.setattr(app, "ExecutorHandle", lambda **_kwargs: executor)
        monkeypatch.setattr(app, "ExecutorHealth", lambda: object())
        monkeypatch.setattr(app, "Sender", SenderStub)
        monkeypatch.setattr(app, "PollScheduler", SchedulerStub)
        monkeypatch.setattr(app, "health_report_loop", health_loop)
        monkeypatch.setattr(app, "account_worker", worker)
        monkeypatch.setattr(app, "supervised", lambda fn, *args, **kwargs: fn(*args, **kwargs))
        monkeypatch.setattr(app, "_install_signal_handlers", install_signal_handler)
        monkeypatch.setattr(app.aiohttp, "TCPConnector", lambda **_kwargs: object())
        monkeypatch.setattr(app.aiohttp, "ClientSession", lambda **_kwargs: Session())

        await app.main()

        assert stopped == [{"wait": False, "cancel_futures": True}]
        assert set(cleanup) == {"sender", "scheduler", "stats", "health", "worker"}

    run(scenario())
