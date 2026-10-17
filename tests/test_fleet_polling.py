"""Модуль 05 — планировщик опроса парка.

Без сети и без реального времени: цикл событий asyncio работает на виртуальных часах
(VirtualLoop), Reddit — заглушка с потоком комментариев фиксированной скорости.
"""
import asyncio
import json
import random
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import fleet_polling as fp  # noqa: E402
from scraper.fleet_polling import (  # noqa: E402
    ACTIVE, AUTH_FAILED, BLOCKED_NODE, COOLDOWN, DEGRADED, DISABLED, AccountHealth, FleetPollingConfig,
)
from scraper.scheduler import PollOutcome, PollScheduler  # noqa: E402
from scraper.state import SeenCache  # noqa: E402


# ---------------------------------------------------------------- #
#  Симулятор времени
# ---------------------------------------------------------------- #

class VirtualLoop(asyncio.SelectorEventLoop):
    """Цикл событий, у которого время идёт мгновенно: sleep(30) не ждёт 30 секунд."""

    def __init__(self):
        super().__init__()
        self._vt = 0.0
        orig = self._selector.select

        def select(timeout=None):
            if timeout:
                self._vt += timeout
            return orig(0)

        self._selector.select = select

    def time(self):
        return self._vt


def simulate(coro_fn):
    loop = VirtualLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_fn(loop))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def make_config(**fleet):
    return {
        "stream_start_interval_seconds": 1.5,
        "stream_min_interval_seconds": 0.4,
        "stream_max_interval_seconds": 6,
        "overlap_target_low": 0.25,
        "overlap_target_high": 0.5,
        "full_page_threshold": 90,
        "fleet_polling": fleet,
    }


# ---------------------------------------------------------------- #
#  Заглушка Reddit + «воркер» с тем же контрактом, что у account_worker
# ---------------------------------------------------------------- #

class StubReddit:
    """Поток: комментарий с номером k появляется в момент k / rate. Страница — 100 штук,
    листаем вглубь не более 10 страниц (как настоящий листинг)."""

    def __init__(self, rate, throttle_gap=0.0):
        self.rate = rate
        self.throttle_gap = throttle_gap   # опрос чаще раза в N с с одного аккаунта -> пустой листинг
        self.auth_fail_from: dict[str, float] = {}
        self.last_req: dict[str, float] = {}
        self.requests = 0
        self.empty_served = 0

    def produced(self, now):
        return int(now * self.rate)

    def begin(self, name, now):
        self.requests += 1
        last = self.last_req.get(name)
        self.last_req[name] = now
        if now >= self.auth_fail_from.get(name, float("inf")):
            return "auth"
        if self.throttle_gap and last is not None and now - last < self.throttle_gap:
            self.empty_served += 1
            return "empty"
        return "ok"

    def page(self, now, k):
        newest = self.produced(now)
        hi = newest - 1 - k * 100
        return list(range(hi, max(-1, hi - 100), -1))


async def drive(sched, name, reddit, seen, collected, issues, loop, latency=0.3):
    """Один аккаунт: ждёт слот, делает «запрос», сообщает планировщику итог."""
    slot = sched.register(name)
    h = slot.health
    try:
        while True:
            await slot.queue.get()
            issues.setdefault(name, []).append(loop.time())
            kind, outcome, cooldown, sent = None, None, 0.0, 0
            try:
                cfg, now = sched.cfg(), sched.now()
                await asyncio.sleep(latency)
                verdict = reddit.begin(name, loop.time())
                if verdict == "auth":
                    kind = "auth"
                    cooldown = 1.0
                    h.on_auth_error(now, cfg, 401)
                elif verdict == "empty":
                    kind = "empty"
                    cooldown = h.on_empty(now, cfg)
                else:
                    kind = "ok"
                    h.on_ok(now, cfg)
                    first_len = first_dupes = pages = 0
                    overlap = False
                    new = []
                    for k in range(10):
                        ids = reddit.page(loop.time(), k)
                        pages += 1
                        dupes = [i for i in ids if i in seen]
                        if k == 0:
                            first_len, first_dupes = len(ids), len(dupes)
                        new += [i for i in ids if i not in seen]
                        if dupes:
                            overlap = True
                            break
                    for i in new:
                        seen.add(i)
                        collected[i] = loop.time()
                    sent = len(new)
                    outcome = PollOutcome(first_len, first_dupes, pages, overlap)
            finally:
                sched.release(slot, outcome, cooldown, sent, kind)
    finally:
        sched.unregister(slot)


async def run_fleet(loop, config, n, reddit, duration, on_tick=None, seed=1):
    sched = PollScheduler(config.get, clock=loop.time, rng=random.Random(seed))
    seen: set = set()
    collected: dict = {}
    issues: dict = {}
    tasks = [asyncio.create_task(sched.run())]
    for i in range(1, n + 1):
        tasks.append(asyncio.create_task(drive(sched, f"account_{i}", reddit, seen, collected, issues, loop)))
    if on_tick:
        tasks.append(asyncio.create_task(on_tick(sched, loop)))
    await asyncio.sleep(duration)
    snap = sched.snapshot()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return {"sched": sched, "collected": collected, "issues": issues, "snap": snap}


def lost(collected, reddit, t1, t2):
    """Сколько комментариев, появившихся в потоке между t1 и t2, мы не собрали."""
    a, b = reddit.produced(t1), reddit.produced(t2)
    return sum(1 for i in range(a, b) if i not in collected)


# ---------------------------------------------------------------- #
#  Конфиг
# ---------------------------------------------------------------- #

def test_repo_config_matches_spec():
    """config.yaml содержит секцию fleet_polling ровно с значениями из ТЗ."""
    conf = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg = FleetPollingConfig.from_getter(conf.get)
    assert cfg.mode == "round_robin"
    assert (cfg.pause_min_seconds, cfg.pause_max_seconds) == (3, 7)
    assert cfg.jitter_ratio == 0.2
    assert cfg.target_fleet_rate is None            # auto
    assert (cfg.cooldown_base_seconds, cfg.cooldown_max_seconds) == (30, 900)
    assert cfg.auth_failed_after == 3
    assert conf["fleet_polling"]["target_fleet_rate"] == "auto"


def test_config_garbage_falls_back_to_defaults():
    d = FleetPollingConfig()
    for raw in ("мусор", 5, ["a"], None):
        assert FleetPollingConfig.from_getter({"fleet_polling": raw}.get) == d
    bad = {"fleet_polling": {"pause_min_seconds": "abc", "pause_max_seconds": -4,
                             "auth_failed_after": 0, "cooldown_base_seconds": None,
                             "target_fleet_rate": -3, "mode": "turbo", "disabled_accounts": "x"}}
    cfg = FleetPollingConfig.from_getter(bad.get)
    assert cfg.pause_min_seconds == 3 and cfg.pause_max_seconds >= cfg.pause_min_seconds
    assert cfg.auth_failed_after == 3 and cfg.cooldown_base_seconds == 30
    assert cfg.target_fleet_rate is None and cfg.mode == "round_robin" and cfg.disabled_accounts == ()


def test_config_rate_pause_order_and_legacy_jitter():
    cfg = FleetPollingConfig.from_getter({"fleet_polling": {"pause_min_seconds": 9, "pause_max_seconds": 4,
                                                            "target_fleet_rate": 2}}.get)
    assert cfg.pause_min_seconds == 9 and cfg.pause_max_seconds == 9   # max не меньше min
    assert cfg.target_fleet_rate == 2.0
    # старый ключ stream_jitter_ratio работает, пока в fleet_polling своего нет
    assert FleetPollingConfig.from_getter({"stream_jitter_ratio": 0.35}.get).jitter_ratio == 0.35
    assert FleetPollingConfig.from_getter(
        {"stream_jitter_ratio": 0.35, "fleet_polling": {"jitter_ratio": 0.1}}.get).jitter_ratio == 0.1


def test_pause_is_random_within_bounds_and_zero_when_disabled():
    cfg = FleetPollingConfig()
    rng = random.Random(7)
    vals = [cfg.pick_pause(rng) for _ in range(500)]
    assert min(vals) >= 3 and max(vals) <= 7 and len({round(v, 3) for v in vals}) > 100
    off = FleetPollingConfig(pause_min_seconds=0, pause_max_seconds=0)
    assert off.pick_pause() == 0.0


# ---------------------------------------------------------------- #
#  Машина состояний
# ---------------------------------------------------------------- #

CFG0 = FleetPollingConfig(jitter_ratio=0.0)   # без джиттера: числа в тестах точные


def test_exp_cooldown_and_retry_after():
    h = AccountHealth("a")
    got = [h.on_rate_limited(100.0, CFG0) for _ in range(7)]
    assert got == [30, 60, 120, 240, 480, 900, 900]            # base*2^(n-1), потолок 900
    assert h.state == COOLDOWN and h.rate_streak == 7
    # Retry-After сервера главнее, но не отменяет экспоненту
    h2 = AccountHealth("b")
    assert h2.on_rate_limited(0.0, CFG0, retry_after=45) == 45
    assert h2.on_rate_limited(0.0, CFG0, retry_after=10) == 60
    assert h2.on_rate_limited(0.0, CFG0, retry_after=2000) == 2000


def test_cooldown_expires_and_success_resets():
    h = AccountHealth("a")
    pause = h.on_rate_limited(10.0, CFG0)
    assert h.effective_state(10.0 + pause - 1) == COOLDOWN and not h.in_rotation(10.0 + pause - 1)
    assert h.effective_state(10.0 + pause) == ACTIVE and h.in_rotation(10.0 + pause)
    h.on_ok(50.0, CFG0)
    assert h.state == ACTIVE and h.rate_streak == 0
    assert h.on_rate_limited(60.0, CFG0) == 30                 # серия началась заново


def test_auth_failed_after_n_errors_and_recovery():
    h = AccountHealth("a")
    assert h.on_auth_error(1.0, CFG0, 401) == ACTIVE
    assert h.on_auth_error(2.0, CFG0, 403) == ACTIVE
    assert h.on_auth_error(3.0, CFG0, 401) == AUTH_FAILED
    assert not h.in_rotation(3.0)
    h.on_ok(200.0, CFG0)                       # проба прошла (новые cookies)
    assert h.state == ACTIVE and h.auth_streak == 0
    # успех в середине серии обнуляет счётчик
    h.on_auth_error(1.0, CFG0, 401); h.on_auth_error(2.0, CFG0, 401)
    h.on_ok(3.0, CFG0)
    assert h.on_auth_error(4.0, CFG0, 401) == ACTIVE


def test_degraded_after_empty_and_recover_needs_two_ok():
    h = AccountHealth("a")
    pauses = [h.on_empty(float(i), CFG0) for i in range(3)]
    assert h.state == DEGRADED
    assert all(3 * 0.8 <= p <= 60 * 1.3 for p in pauses) and pauses[2] > pauses[0]
    h.on_ok(10.0, CFG0)
    assert h.state == DEGRADED                  # одного успеха мало
    h.on_ok(11.0, CFG0)
    assert h.state == ACTIVE and h.empty_streak == 0
    h.on_empty(12.0, CFG0); h.on_ok(13.0, CFG0); h.on_empty(14.0, CFG0)
    assert h.state == ACTIVE                    # «подряд» — успех между пустыми сбрасывает серию


def test_emergency_ratelimit_only_when_remaining_is_low():
    h = AccountHealth("a")
    assert h.on_emergency_ratelimit(0.0, CFG0, None) == 0.0
    assert h.on_emergency_ratelimit(0.0, CFG0, {"remaining": 80, "reset": 500}) == 0.0
    assert h.state == ACTIVE                    # обычные заголовки темп не трогают
    assert h.on_emergency_ratelimit(5.0, CFG0, {"remaining": 2, "reset": 40}) == 41.0
    assert h.state == COOLDOWN and h.cooldown_until == 46.0
    assert h.on_emergency_ratelimit(6.0, CFG0, {"remaining": 0, "reset": 99999}, max_interval=900) == 900


def test_blocked_node_disabled_and_reset():
    h = AccountHealth("a")
    h.mark_blocked_node(1.0)
    assert h.state == BLOCKED_NODE and not h.in_rotation(1.0)
    h.on_auth_error(2.0, CFG0, 403); h.on_auth_error(3.0, CFG0, 403); h.on_auth_error(4.0, CFG0, 403)
    assert h.state == BLOCKED_NODE              # auth_failed не затирает blocked_node
    h.reset(5.0, "node_replaced")
    assert h.state == ACTIVE and h.auth_streak == 0
    h.set_disabled(6.0, True)
    h.on_ok(7.0, CFG0); h.on_rate_limited(8.0, CFG0); h.mark_blocked_node(9.0)
    assert h.state == DISABLED                  # выключенный не оживает от событий
    h.set_disabled(10.0, False)
    assert h.state == ACTIVE


def test_state_transitions_are_logged():
    h = AccountHealth("a")
    h.on_rate_limited(1.0, CFG0); h.on_ok(2.0, CFG0)
    assert [(a, b) for _, a, b in h.transitions] == [(ACTIVE, COOLDOWN), (COOLDOWN, ACTIVE)]


# ---------------------------------------------------------------- #
#  Планировщик
# ---------------------------------------------------------------- #

def test_release_applies_pause_floor_and_cooldowns():
    async def body(loop):
        s = PollScheduler(make_config().get, clock=loop.time, rng=random.Random(3))
        slot = s.register("a")
        for _ in range(200):
            now = loop.time()
            s.release(slot, None, 0.0, 0, "ok")
            assert 3.0 <= slot.not_before - now <= 7.0       # пауза 3–7 с на каждый запрос
        s.release(slot, None, 50.0, 0, "ok")                 # собственный кулдаун длиннее паузы
        assert slot.not_before - loop.time() == pytest.approx(50.0)
        slot.health.on_rate_limited(loop.time(), CFG0)       # аварийный тормоз
        s.release(slot, None, 0.0, 0, "rate_or_server")
        assert slot.not_before - loop.time() == pytest.approx(30.0)

    simulate(body)


def test_skipped_cycles_are_not_polls():
    async def body(loop):
        s = PollScheduler(make_config().get, clock=loop.time)
        slot = s.register("a")
        s.release(slot, None, 5.0, 0, "skipped")
        s.release(slot, None, 0.0, 3, "ok")
        assert s.totals["polls"] == 1 and s.totals["skipped"] == 1 and s.totals["unique"] == 3

    simulate(body)


def test_round_robin_visits_everyone_in_turn():
    async def body(loop):
        r = StubReddit(5)
        res = await run_fleet(loop, make_config(target_fleet_rate=1.0, pause_min_seconds=0, pause_max_seconds=0),
                              n=5, reddit=r, duration=60)
        first_round = sorted(((ts[0], n) for n, ts in res["issues"].items()))
        assert [n for _, n in first_round] == [f"account_{i}" for i in range(1, 6)]
        counts = [len(v) for v in res["issues"].values()]
        assert max(counts) - min(counts) <= 1               # нагрузка поровну

    simulate(body)


def test_acceptance_64_accounts_rate_2_each_polls_every_3s_at_most_and_30s_on_average():
    async def body(loop):
        r = StubReddit(20)
        res = await run_fleet(loop, make_config(target_fleet_rate=2), n=64, reddit=r, duration=1200)
        gaps = []
        for ts in res["issues"].values():
            gaps += [b - a for a, b in zip(ts, ts[1:])]
        assert min(gaps) >= 3.0, f"аккаунт опрошен чаще нижней границы паузы: {min(gaps):.2f} с"
        mean = sum(gaps) / len(gaps)
        assert 28 <= mean <= 36, f"средний интервал на аккаунт {mean:.1f} с (ожидали ~30)"
        total = sum(len(v) for v in res["issues"].values())
        assert total / 1200 == pytest.approx(2.0, rel=0.05)    # T_fleet = 2 запроса/с
        snap = res["snap"]
        assert snap["accounts_healthy"] == 64 and snap["warning"] is None
        assert snap["interval_per_account"] == pytest.approx(32.0, rel=0.05)

    simulate(body)


def test_auth_failed_account_load_is_redistributed_without_loss():
    async def body(loop):
        r = StubReddit(20)
        r.auth_fail_from["account_3"] = 100.0               # с 100-й секунды 401 на каждый запрос
        r.auth_fail_from["account_7"] = 100.0
        res = await run_fleet(loop, make_config(), n=16, reddit=r, duration=600)
        sched, snap = res["sched"], res["snap"]
        states = {a["name"]: a["state"] for a in snap["accounts"]}
        assert states["account_3"] == AUTH_FAILED and states["account_7"] == AUTH_FAILED
        assert snap["accounts_healthy"] == 14
        # после перехода в auth_failed — только редкие пробы (раз в ~recovery_probe_seconds)
        late = [t for t in res["issues"]["account_3"] if t > 160]
        assert len(late) <= (600 - 160) / 120 * 1.5 + 1, late
        # ни одного потерянного комментария на всём потоке (кроме разгона и самого хвоста)
        assert lost(res["collected"], r, 30, 580) == 0
        # скорость уникальных после падения не просела
        before = sum(1 for t in res["collected"].values() if 30 <= t < 100) / 70
        after = sum(1 for t in res["collected"].values() if 200 <= t < 600) / 400
        assert after >= before * 0.95
        assert sched.totals["auth"] >= 6

    simulate(body)


def test_new_cookies_return_account_to_rotation_via_probe():
    async def body(loop):
        r = StubReddit(10)
        r.auth_fail_from["account_1"] = 50.0

        async def fix_cookies(sched, loop):
            await asyncio.sleep(300)
            del r.auth_fail_from["account_1"]                # «обновили cookies» — пробы снова проходят

        res = await run_fleet(loop, make_config(), n=8, reddit=r, duration=700, on_tick=fix_cookies)
        states = {a["name"]: a["state"] for a in res["snap"]["accounts"]}
        assert states["account_1"] == ACTIVE
        assert any(t > 450 for t in res["issues"]["account_1"])    # вернулся в карусель
        h = [s for s in res["sched"].slots if s.name == "account_1"]  # слот жив до конца
        assert not h or h[0].health.state == ACTIVE

    simulate(body)


def test_unique_rate_not_lower_than_old_scheduler_and_no_more_empty_listings():
    """Сравнение на одном и том же потоке: «старый» планировщик = без нижней границы паузы
    (pause 0..0, остальное — та же адаптация темпа). Reddit мягко троттлит аккаунт,
    который опрашивают чаще раза в 3 с — отдаёт пустой листинг."""
    async def scenario(loop, **fleet):
        r = StubReddit(100, throttle_gap=3.0)
        res = await run_fleet(loop, make_config(**fleet), n=4, reddit=r, duration=600)
        got = sum(1 for t in res["collected"].values() if 60 <= t < 600)
        return {"unique": got, "empty": r.empty_served, "lost": lost(res["collected"], r, 60, 590),
                "snap": res["snap"]}

    async def body(loop):
        return await scenario(loop, pause_min_seconds=0, pause_max_seconds=0)

    async def body_new(loop):
        return await scenario(loop)

    old = simulate(body)
    new = simulate(body_new)
    assert old["empty"] > 0, "сценарий не показывает проблему старого планировщика"
    assert new["empty"] == 0, "после перехода пустые листинги не должны появляться"
    assert new["unique"] >= old["unique"] * 0.98
    assert new["lost"] == 0
    # парк из 4 аккаунтов мал для такого потока при паузе 3–7 с -> панель предупреждает
    assert new["snap"]["warning"] == "not_enough_accounts" or new["snap"]["interval_per_account"] >= 5


def test_unique_rate_same_as_old_with_full_fleet_and_no_warning():
    async def scenario(loop, **fleet):
        r = StubReddit(30)
        res = await run_fleet(loop, make_config(**fleet), n=64, reddit=r, duration=600)
        return {"unique": sum(1 for t in res["collected"].values() if 60 <= t < 600),
                "lost": lost(res["collected"], r, 60, 590), "snap": res["snap"]}

    async def b_old(loop):
        return await scenario(loop, pause_min_seconds=0, pause_max_seconds=0)

    async def b_new(loop):
        return await scenario(loop)

    old, new = simulate(b_old), simulate(b_new)
    assert new["unique"] >= old["unique"] * 0.98
    assert new["lost"] == 0 and old["lost"] == 0
    assert new["snap"]["warning"] is None


def test_not_enough_accounts_warning_and_recovery():
    async def body(loop):
        s = PollScheduler(make_config(target_fleet_rate=2).get, clock=loop.time)
        for i in range(8):
            s.register(f"account_{i}")
        snap = s.snapshot()
        assert snap["warning"] == "not_enough_accounts"          # 8 / 2 = 4 с < средней паузы 5 с
        assert snap["interval_per_account"] == pytest.approx(4.0)
        for i in range(8, 40):
            s.register(f"account_{i}")
        assert s.snapshot()["warning"] is None                   # 40 / 2 = 20 с
        # все аккаунты вышли из карусели
        for sl in s.slots:
            sl.health.mark_blocked_node(loop.time())
        assert s.snapshot()["warning"] == "not_enough_accounts"

    simulate(body)


def test_disabled_accounts_are_excluded_via_config_hot_reload():
    async def body(loop):
        conf = make_config(target_fleet_rate=1.0, pause_min_seconds=0, pause_max_seconds=0)
        r = StubReddit(5)

        async def toggle(sched, loop):
            await asyncio.sleep(100)
            conf["fleet_polling"]["disabled_accounts"] = ["account_2"]

        res = await run_fleet(loop, conf, n=4, reddit=r, duration=300, on_tick=toggle)
        issued_after = [t for t in res["issues"]["account_2"] if t > 110]
        assert issued_after == []
        assert res["snap"]["states"][DISABLED] == 1
        assert res["snap"]["accounts_healthy"] == 3

    simulate(body)


def test_demand_none_stops_polling_but_keeps_rotation():
    async def body(loop):
        mode = {"v": 1}
        s = PollScheduler(make_config(target_fleet_rate=2, pause_min_seconds=0, pause_max_seconds=0).get,
                          demand=lambda: mode["v"], clock=loop.time, rng=random.Random(1))
        r = StubReddit(5)
        seen, collected, issues = set(), {}, {}
        tasks = [asyncio.create_task(s.run())] + [
            asyncio.create_task(drive(s, f"account_{i}", r, seen, collected, issues, loop)) for i in range(4)]
        await asyncio.sleep(50)
        n1 = sum(len(v) for v in issues.values())
        mode["v"] = 0                                    # буфер полон — Reddit не трогаем
        await asyncio.sleep(50)
        n2 = sum(len(v) for v in issues.values())
        assert n2 - n1 <= 1
        mode["v"] = 1
        await asyncio.sleep(50)
        n3 = sum(len(v) for v in issues.values())
        assert n3 - n2 >= 80                             # ~2/с после возобновления
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    simulate(body)


def test_urgent_demand_raises_rate_only_in_auto_mode():
    async def body(loop):
        auto = PollScheduler(make_config().get, clock=loop.time)
        assert auto.fleet_interval(auto.cfg(), 2) == 0.4       # срочно: максимальный темп
        assert auto.fleet_interval(auto.cfg(), 1) == 1.5
        fixed = PollScheduler(make_config(target_fleet_rate=2).get, clock=loop.time)
        assert fixed.fleet_interval(fixed.cfg(), 2) == 0.5     # число — решение оператора

    simulate(body)


def test_snapshot_is_json_and_written_atomically(tmp_path):
    async def body(loop):
        s = PollScheduler(make_config().get, clock=loop.time)
        for i in range(3):
            s.register(f"account_{i}")
        path = tmp_path / "state" / "fleet_polling.json"
        s._write_snapshot(path, s.snapshot())
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema"] == 1 and data["accounts_total"] == 3
        assert set(data["states"]) == set(fp.ALL_STATES)
        assert not list(path.parent.glob("*.tmp"))

    simulate(body)


# ---------------------------------------------------------------- #
#  Настоящий account_worker с заглушкой fetch_comments
# ---------------------------------------------------------------- #

def test_account_worker_reports_states_to_scheduler(tmp_path, monkeypatch):
    pytest.importorskip("curl_cffi")
    pytest.importorskip("aiohttp")
    from scraper import worker as w
    from scraper.config import ConfigStore
    from scraper.models import FetchResult

    cookie = tmp_path / "account_1.json"
    cookie.write_text(json.dumps([{"name": "reddit_session", "value": "x"}]), encoding="utf-8")
    monkeypatch.setattr(w, "load_device_profile", lambda name: None)
    monkeypatch.setattr(w.identity, "read_request", lambda name: None)

    script = {"mode": "401"}
    calls = []

    async def fake_fetch(session, base_url, subs, limit, curl_session, timeout, name, *a, **kw):
        calls.append(asyncio.get_running_loop().time())
        if script["mode"] == "401":
            return FetchResult(status=401, error_kind="auth")
        if script["mode"] == "429":
            return FetchResult(status=429, error_kind="rate_or_server", retry_after=None)
        if script["mode"] == "empty":
            return FetchResult(status=200, error_kind="empty")
        return FetchResult(comments=[], status=200, ratelimit={"remaining": 1, "reset": 10})

    monkeypatch.setattr(w, "fetch_comments", fake_fetch)

    class StubSender:
        backlog = 0

        async def put_many(self, items):
            return len(items)

    async def body(loop):
        data = make_config(target_fleet_rate=2)
        data["cookie_reload_check_interval_seconds"] = 0
        data["rotation"] = {"enabled": False}
        conf = ConfigStore(tmp_path / "config.yaml", 5)
        conf._data = data
        sched = PollScheduler(conf.get, clock=loop.time, rng=random.Random(1))
        account = {"name": "account_1", "cookie_file": str(cookie), "proxy_port": 1,
                   "impersonate": "chrome", "user_agent": "UA"}
        task = asyncio.create_task(w.account_worker(account, conf, StubSender(), SeenCache(100), sched, None, None))
        runner = asyncio.create_task(sched.run())
        await asyncio.sleep(0.1)
        slot = sched.slots[0]

        await asyncio.sleep(40)                                   # 401 подряд -> auth_failed на 3-м
        assert slot.health.state == AUTH_FAILED and slot.health.auth_streak >= 3
        n = len(calls)
        await asyncio.sleep(300)                                  # дальше — только пробы раз в ~120 с
        assert 1 <= len(calls) - n <= 4

        script["mode"] = "ok"                                     # cookies починили: проба проходит
        await asyncio.sleep(400)
        assert slot.health.state in (ACTIVE, COOLDOWN)            # remaining=1 -> аварийный тормоз
        assert sched.totals["ok"] >= 1

        script["mode"] = "429"
        await asyncio.sleep(120)
        assert slot.health.rate_streak >= 1 and sched.totals["rate_or_server"] >= 1

        script["mode"] = "empty"
        await asyncio.sleep(600)
        assert slot.health.state == DEGRADED and sched.totals["empty"] >= 3

        for t in (task, runner):
            t.cancel()
        await asyncio.gather(task, runner, return_exceptions=True)

    simulate(body)