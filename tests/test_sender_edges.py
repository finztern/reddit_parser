import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.constants import SERVER_HARD_BATCH_LIMIT  # noqa: E402
from scraper.sender import Sender  # noqa: E402
from scraper.state import SeenCache  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def item(item_id, age=0):
    return {
        "external_id": item_id,
        "_created_utc": time.time() - age,
        "content": item_id,
    }


def make_sender(**config):
    defaults = {
        "store_endpoint": "https://collector.example/store_items",
        "max_age_seconds": 10,
        "target_rate_per_second": 100,
        "queue_control": {
            "enabled": False,
            "send_batch_size": 2,
            "inflight_target": 2,
            "queue_target": 4,
            "buffer_target_factor": 1.5,
            "buffer_max_factor": 3,
            "min_send_gap_seconds": 0,
        },
    }
    defaults.update(config)
    return Sender(defaults, SeenCache(20), object(), logging.getLogger("sender-test"))


@pytest.mark.parametrize("value,expected", [
    (2, 2.0),
    ("3.5", 3.5),
    ("invalid", None),
    (None, None),
])
def test_sender_numeric_parser(value, expected):
    from scraper.sender import _num

    assert _num(value) == expected


def test_sender_config_limits_batch_and_reports_queue_information():
    sender = make_sender(queue_control={
        "send_batch_size": SERVER_HARD_BATCH_LIMIT + 100,
        "inflight_target": 9,
        "queue_target": 10,
        "buffer_target_factor": 1.5,
        "buffer_max_factor": 2,
    })

    assert sender._batch() == SERVER_HARD_BATCH_LIMIT
    assert sender._fill_target() == int(SERVER_HARD_BATCH_LIMIT * 1.5)
    assert sender._buffer_max() == SERVER_HARD_BATCH_LIMIT * 2
    assert sender.queue_info() == {
        "size": None, "max": None, "inflight": None, "degraded": None,
        "action": None, "buffer": 0,
    }
    sender._status = {"bpipe_queue": 5, "bpipe_max": 20, "bpipe_inflight": 3,
                      "degraded": 0, "action": "normal"}
    assert sender.queue_info() == {
        "size": 5, "max": 20, "inflight": 3, "degraded": False,
        "action": "normal", "buffer": 0,
    }


def test_sender_expire_releases_claim_but_retains_fresh_items():
    async def scenario():
        seen = SeenCache(10)
        assert await seen.try_claim("old")
        assert await seen.try_claim("fresh")
        sender = make_sender(max_age_seconds=10)
        sender.seen = seen
        sender._buf = [item("old", age=11), item("fresh", age=2)]

        await sender._expire()

        assert [entry["external_id"] for entry in sender._buf] == ["fresh"]
        assert not seen.contains("old")
        assert seen.contains("fresh")
        assert sender._w_expired == 1

    run(scenario())


def test_sender_eviction_releases_oldest_claims():
    async def scenario():
        seen = SeenCache(10)
        for item_id in ("old", "middle", "new"):
            assert await seen.try_claim(item_id)
        sender = make_sender(queue_control={
            "enabled": False,
            "send_batch_size": 1,
            "inflight_target": 1,
            "queue_target": 1,
            "buffer_target_factor": 1,
            "buffer_max_factor": 2,
        })
        sender.seen = seen
        await sender.put_many([item("old", age=3), item("middle", age=2), item("new")])

        assert [entry["external_id"] for entry in sender._buf] == ["new", "middle"]
        assert not seen.contains("old")
        assert seen.contains("middle") and seen.contains("new")
        assert sender._w_evicted == 1

    run(scenario())


def test_sender_fetch_handles_non_success_non_dict_and_connection_errors():
    class Response:
        def __init__(self, status, result):
            self.status = status
            self.result = result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def json(self, **_):
            return self.result

    class Session:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error

        def get(self, *_args, **_kwargs):
            if self.error:
                raise self.error
            return self.response

    async def scenario():
        log = logging.getLogger("sender-fetch-test")
        assert await Sender(make_sender().config, SeenCache(1), Session(Response(503, {})), log)._fetch("url", 1) is None
        assert await Sender(make_sender().config, SeenCache(1), Session(Response(200, [])), log)._fetch("url", 1) is None
        assert await Sender(make_sender().config, SeenCache(1), Session(Response(200, {"size": 1})), log)._fetch("url", 1) == {"size": 1}
        assert await Sender(make_sender().config, SeenCache(1), Session(error=aiohttp.ClientError()), log)._fetch("url", 1) is None

    run(scenario())


def test_sender_releases_claims_for_missing_endpoint_and_unreturned_flags(monkeypatch):
    async def scenario():
        seen = SeenCache(10)
        assert await seen.try_claim("missing")
        sender = make_sender(store_endpoint="")
        sender.seen = seen
        assert await sender._send([item("missing")]) == 0
        assert not seen.contains("missing")

        for item_id in ("sent", "unreturned"):
            assert await seen.try_claim(item_id)
        sender = make_sender()
        sender.seen = seen

        async def incomplete(*_args):
            return [True]

        monkeypatch.setattr("scraper.sender.send_batch_to_store", incomplete)
        assert await sender._send([item("sent"), item("unreturned")]) == 1
        assert seen.contains("sent") and not seen.contains("unreturned")

    run(scenario())


def test_sender_releases_failed_item_that_expired_during_send(monkeypatch):
    async def scenario():
        seen = SeenCache(10)
        assert await seen.try_claim("expired")
        sender = make_sender(max_age_seconds=1)
        sender.seen = seen

        async def reject(*_args):
            return [False]

        monkeypatch.setattr("scraper.sender.send_batch_to_store", reject)
        assert await sender._send([item("expired", age=3)]) == 0
        assert not seen.contains("expired")
        assert sender._w_failed == 1
        assert sender._buf == []

    run(scenario())


def test_sender_flush_updates_metrics_and_queue_estimate(monkeypatch):
    class Metrics:
        def __init__(self):
            self.calls = []

        def record_send(self, sent, failed):
            self.calls.append((sent, failed))

    async def scenario():
        seen = SeenCache(10)
        assert await seen.try_claim("one")
        assert await seen.try_claim("two")
        metrics = Metrics()
        sender = make_sender()
        sender.seen = seen
        sender.metrics = metrics
        sender._status = {"bpipe_queue": 3}

        async def send(items):
            for entry in items:
                await seen.confirm(entry["external_id"])
            return 1

        monkeypatch.setattr(sender, "_send", send)
        await sender.put_many([item("one"), item("two")])
        await sender._flush(2, partial=True)

        assert sender._status["bpipe_queue"] == 4
        assert metrics.calls == [(1, 1)]
        assert sender._w_batches == 1 and sender._w_partial == 1
        assert sender._w_sent == 1

    run(scenario())


def test_sender_fallback_flushes_aged_partial_batch(monkeypatch):
    async def scenario():
        seen = SeenCache(10)
        assert await seen.try_claim("aged")
        sender = make_sender(
            max_age_seconds=10,
            queue_control={
                "enabled": False,
                "send_batch_size": 2,
                "inflight_target": 2,
                "queue_target": 2,
                "flush_age_fraction": 0.6,
                "min_send_gap_seconds": 0,
            },
        )
        sender.seen = seen
        await sender.put_many([item("aged", age=7)])
        calls = []

        async def flush(count, partial):
            calls.append((count, partial))

        monkeypatch.setattr(sender, "_flush", flush)
        await sender._tick()
        assert calls == [(2, True)]
        assert not sender._starving

    run(scenario())


def test_sender_enters_fallback_after_queue_endpoint_is_stale(monkeypatch):
    async def scenario():
        seen = SeenCache(10)
        for item_id in ("one", "two"):
            assert await seen.try_claim(item_id)
        sender = make_sender(queue_control={
            "enabled": True,
            "url": "https://collector.example/queue",
            "send_batch_size": 2,
            "inflight_target": 2,
            "queue_target": 2,
            "stale_seconds": 5,
            "min_send_gap_seconds": 0,
        })
        sender.seen = seen
        sender._started = 0
        await sender.put_many([item("one"), item("two")])
        calls = []

        async def fetch(*_args):
            return None

        async def flush(count, partial):
            calls.append((count, partial))

        monkeypatch.setattr(sender, "_fetch", fetch)
        monkeypatch.setattr("scraper.sender.time.monotonic", lambda: 10)
        monkeypatch.setattr(sender, "_flush", flush)
        await sender._tick()

        assert sender._fallback
        assert calls == [(2, False)]
        assert not sender._starving

    run(scenario())


def test_sender_logs_and_resets_window_statistics():
    sender = make_sender()
    sender._w_batches = 3
    sender._w_sent = 5
    sender._w_partial = 1
    sender._w_failed = 2
    sender._w_expired = 4
    sender._w_evicted = 6
    sender._w_enq = 8
    sender._w_starved_ticks = 7
    sender._w_ticks = 9

    sender._log_window(elapsed=0)

    assert (sender._w_batches, sender._w_sent, sender._w_partial) == (0, 0, 0)
    assert (sender._w_failed, sender._w_expired, sender._w_evicted) == (0, 0, 0)
    assert (sender._w_enq, sender._w_starved_ticks, sender._w_ticks) == (0, 0, 0)


def test_sender_run_surfaces_cancellation_after_current_tick(monkeypatch):
    async def scenario():
        sender = make_sender()
        calls = []

        async def tick():
            calls.append("tick")

        async def sleep(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(sender, "_tick", tick)
        monkeypatch.setattr("scraper.sender.asyncio.sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await sender.run()
        assert calls == ["tick"]

    run(scenario())
