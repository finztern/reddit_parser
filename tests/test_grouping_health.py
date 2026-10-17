import asyncio
import csv
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.grouping import (  # noqa: E402
    assign_to_accounts,
    bucket_by_tier,
    build_account_groups,
    build_account_groups_from_ranked,
    load_ranked_subreddits,
)
from scraper.health import ExecutorHandle, ExecutorHealth, health_report_loop  # noqa: E402


def test_load_ranked_subreddits_normalizes_and_sorts(tmp_path):
    csv_path = tmp_path / "ranked.csv"
    csv_path.write_text(
        "rank,subreddit\n3, r/AskReddit \n1,news\n2, \n",
        encoding="utf-8",
    )

    assert load_ranked_subreddits(csv_path) == [(1, "news"), (3, "AskReddit")]


def test_load_ranked_subreddits_reports_invalid_rank(tmp_path):
    csv_path = tmp_path / "ranked.csv"
    csv_path.write_text("rank,subreddit\nnot-a-number,news\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_ranked_subreddits(csv_path)


def test_assign_to_accounts_balances_weight_and_handles_no_accounts():
    ranked = [(1, "hot"), (2, "warm"), (3, "cool"), (4, "cold")]

    assert assign_to_accounts(ranked, 0) == []
    buckets = assign_to_accounts(ranked, 2)
    assert sorted(sub for bucket in buckets for sub in bucket) == ranked
    loads = [sum(1 / rank**0.5 for rank, _ in bucket) for bucket in buckets]
    assert max(loads) - min(loads) < 1 / 2**0.5


def test_bucket_by_tier_chunks_and_clamps_group_sizes():
    subs = [(1, "hot-a"), (2, "hot-b"), (3, "hot-c"),
            (11, "mid-a"), (12, "mid-b"), (21, "cold-a")]

    groups = bucket_by_tier(subs, 3, 20, hot_group_size=2,
                            medium_group_size=0, cold_group_size=10)

    assert groups == [
        {"tier": "hot", "subs": ["hot-a", "hot-b"]},
        {"tier": "hot", "subs": ["hot-c"]},
        {"tier": "medium", "subs": ["mid-a"]},
        {"tier": "medium", "subs": ["mid-b"]},
        {"tier": "cold", "subs": ["cold-a"]},
    ]


def test_build_account_groups_supports_empty_and_csv_input(tmp_path):
    ranked = [(1, "hot"), (2, "warm"), (20, "cold")]
    assert build_account_groups_from_ranked(ranked, []) == {}

    csv_path = tmp_path / "ranked.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["rank", "subreddit"])
        writer.writerows([(2, "warm"), (1, "hot"), (20, "cold")])

    groups = build_account_groups(
        csv_path, ["one", "two"], hot_max_rank=1, medium_max_rank=5,
        hot_group_size=1, medium_group_size=1, cold_group_size=1,
    )
    assert set(groups) == {"one", "two"}
    assert sum(len(group["subs"]) for account in groups.values() for group in account) == 3


def test_executor_handle_swap_replaces_pool_and_shuts_down_old():
    pools = []

    def factory():
        pool = ThreadPoolExecutor(max_workers=1)
        pools.append(pool)
        return pool

    handle = ExecutorHandle(factory, max_workers=1)
    old = handle.current
    handle.swap()

    assert handle.current is pools[-1]
    assert handle.current is not old
    assert handle.swaps == 1
    assert old._shutdown
    handle.shutdown(wait=True)


def test_executor_health_ignores_completions_from_old_generation():
    health = ExecutorHealth()
    old_generation = health.on_submit()
    health.register_swap(leaked_now=1)
    new_generation = health.on_submit()

    health.on_future_done(old_generation)
    assert health.active == 1
    health.on_future_done(new_generation)
    assert health.active == 0
    assert health.leaked_total == 1
    assert health.snapshot(max_workers=4, swaps=2) == (
        "active=0 submitted=1 completed=1 timed_out_waits=0 "
        "leaked_total=1 swaps=2 pool_size=4"
    )
    health.on_wait_timeout()
    assert health.timed_out_waits == 1


class StopHealthLoop(Exception):
    pass


def test_health_report_loop_swaps_when_pool_is_exhausted(monkeypatch):
    async def scenario():
        health = ExecutorHealth()
        for _ in range(2):
            health.on_submit()
        pools = []

        def factory():
            pool = ThreadPoolExecutor(max_workers=2)
            pools.append(pool)
            return pool

        handle = ExecutorHandle(factory, max_workers=2)
        logs = []
        logger = type("Logger", (), {
            "info": lambda _self, *args: logs.append(("info", args)),
            "warning": lambda _self, *args: logs.append(("warning", args)),
            "error": lambda _self, *args: logs.append(("error", args)),
            "critical": lambda _self, *args: logs.append(("critical", args)),
        })()

        sleeps = 0

        async def sleep(_interval):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise StopHealthLoop

        monkeypatch.setattr("scraper.health.asyncio.sleep", sleep)
        task = health_report_loop(health, handle, 1, logger, swap_threshold=1)
        with pytest.raises(StopHealthLoop):
            await task

        assert handle.swaps == 1
        assert health.active == 0
        assert health.leaked_total == 2
        assert any(level == "error" for level, _ in logs)
        handle.shutdown(wait=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("active,max_workers,last_swap,cooldown", [
    (1, 0, 0, 0),
    (1, 2, 0, 0),
    (2, 2, 1e30, 100),
])
def test_health_report_loop_logs_without_swapping(
    monkeypatch, active, max_workers, last_swap, cooldown
):
    async def scenario():
        health = ExecutorHealth()
        for _ in range(active):
            health.on_submit()
        health.last_swap_at = last_swap
        pools = []

        def factory():
            pool = ThreadPoolExecutor(max_workers=2)
            pools.append(pool)
            return pool

        handle = ExecutorHandle(factory, max_workers=max_workers)
        logs = []
        logger = type("Logger", (), {
            "info": lambda _self, *args: logs.append(("info", args)),
            "warning": lambda _self, *args: logs.append(("warning", args)),
            "error": lambda _self, *args: logs.append(("error", args)),
            "critical": lambda _self, *args: logs.append(("critical", args)),
        })()

        sleeps = 0

        async def sleep(_interval):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise StopHealthLoop

        monkeypatch.setattr("scraper.health.asyncio.sleep", sleep)
        with pytest.raises(StopHealthLoop):
            await health_report_loop(
                health, handle, 1, logger, swap_threshold=1,
                swap_cooldown_seconds=cooldown,
            )

        assert handle.swaps == 0
        assert any(level in ("info", "warning") for level, _ in logs)
        handle.shutdown(wait=True)

    asyncio.run(scenario())


def test_health_report_loop_stops_after_fatal_executor_leak(monkeypatch):
    async def scenario():
        health = ExecutorHealth()
        health.on_submit()
        pools = []

        def factory():
            pool = ThreadPoolExecutor(max_workers=1)
            pools.append(pool)
            return pool

        handle = ExecutorHandle(factory, max_workers=1)
        logger = logging.getLogger("health-test")

        sleeps = 0

        async def sleep(_interval):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise StopHealthLoop

        monkeypatch.setattr("scraper.health.asyncio.sleep", sleep)
        with pytest.raises(SystemExit) as error:
            await health_report_loop(
                health, handle, 1, logger, swap_threshold=1,
                fatal_leak_multiplier=1,
            )

        assert error.value.code == 1
        assert handle.swaps == 1
        handle.shutdown(wait=True)

    asyncio.run(scenario())
