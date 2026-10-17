"""Модуль 06 — метрики скрапера. Без сети и реального времени: часы подменяются, события синтетические."""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import metrics as m  # noqa: E402
from scraper.fleet_polling import ACTIVE, AUTH_FAILED, BLOCKED_NODE, COOLDOWN  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make(tmp_path=None, accounts=None, **kw):
    clk = Clock()
    journal = m.EventJournal(tmp_path / "events.jsonl") if tmp_path else m.EventJournal(Path("/nonexistent/x"))
    c = m.MetricsCollector(clock=clk, wall=lambda: 1_700_000_000.0 + (clk.t - 1000.0),
                           state_dir=tmp_path, journal=journal, **kw)
    if accounts:
        c.attach(accounts=accounts)
    return c, clk


# ---------------------------------------------------------------- #
#  StepSeries: окна и значения рядов
# ---------------------------------------------------------------- #

def test_step_series_window_and_expiry():
    s = m.StepSeries(5.0, 180)
    s.add(1000.0, 10)
    s.add(1004.9, 5)       # та же корзина
    s.add(1007.0, 3)       # следующая
    assert s.window_sum(1007.5, 10) == 18
    assert s.window_sum(1020.0, 10) == 0           # окно 10 с уже не захватывает старое
    assert s.window_sum(1020.0, 900) == 18         # а 15 мин — захватывает
    assert s.window_sum(1000.0 + 5.0 * 181, 900) == 0  # старше буфера — забыто


def test_step_series_values_oldest_first_with_gaps():
    s = m.StepSeries(5.0, 180)
    s.add(1000.0, 1)
    s.add(1010.0, 2)
    vals = s.values(1010.0, 4)
    assert vals == [0.0, 1.0, 0.0, 2.0]
    assert len(s.values(1010.0)) == 180


def test_step_series_ring_reuse_resets_cell():
    s = m.StepSeries(5.0, 4)
    s.add(0.0, 7)
    s.add(20.0, 1)         # та же ячейка (корзина 4 -> ячейка 0), старое значение не должно протечь
    assert s.window_sum(20.0, 5) == 1


# ---------------------------------------------------------------- #
#  Скорости, дедуп, коды ответов
# ---------------------------------------------------------------- #

def test_rates_and_dedupe_ratio_over_windows():
    c, clk = make()
    for _ in range(60):                       # 60 с: каждую секунду опрос, 100 получено, 25 уникальных
        c.record_poll("a1", "ok", status=200, requests=2, fetched=100, unique=25, state=ACTIVE)
        clk.advance(1.0)
    w = c.snapshot()["global"]["windows"]
    assert w["1m"]["unique_comments_per_sec"] == pytest.approx(25, rel=0.1)
    assert w["1m"]["comments_fetched_per_sec"] == pytest.approx(100, rel=0.1)
    assert w["1m"]["dedupe_ratio"] == pytest.approx(0.75, abs=1e-6)
    assert w["1m"]["requests_per_sec"] == pytest.approx(2, rel=0.1)
    assert w["total"]["codes"]["200"] == 120
    assert w["total"]["span_s"] == pytest.approx(60, abs=1)


def test_short_window_forgets_old_load_and_total_keeps_it():
    c, clk = make()
    c.record_poll("a1", "ok", 200, requests=1, fetched=50, unique=50, state=ACTIVE)
    clk.advance(120)
    w = c.snapshot()["global"]["windows"]
    assert w["10s"]["unique_comments_per_sec"] == 0
    assert w["10s"]["dedupe_ratio"] is None            # нет данных, а не деление на ноль
    assert w["15m"]["unique_comments_per_sec"] > 0
    assert w["total"]["comments_fetched_per_sec"] > 0


def test_rate_right_after_start_not_underestimated():
    c, clk = make()
    clk.advance(2.0)
    c.record_poll("a1", "ok", 200, fetched=40, unique=40, state=ACTIVE)
    w = c.snapshot()["global"]["windows"]["15m"]
    assert w["unique_comments_per_sec"] == pytest.approx(20, rel=0.01)   # 40 за 2 с аптайма, а не за 900


def test_codes_empty_network_and_send_errors():
    c, clk = make()
    c.record_poll("a1", "ok", 200, requests=3, fetched=10, unique=10, state=ACTIVE)
    c.record_poll("a1", "auth", 403, state=ACTIVE)
    c.record_poll("a2", "rate_or_server", 429, state=ACTIVE)
    c.record_poll("a2", "rate_or_server", 503, state=ACTIVE)
    c.record_poll("a3", "auth", 401, state=ACTIVE)
    c.record_poll("a3", "empty", 200, state=ACTIVE)
    c.record_poll("a3", "network", None, state=ACTIVE)
    c.record_send(sent=90, failed=10)
    w = c.snapshot()["global"]["windows"]["total"]
    assert w["codes"] == {"200": 4, "403": 1, "429": 1, "5xx": 1, "other": 1}
    assert w["empty_listings"] == 1 and w["network_errors"] == 1
    assert w["send_errors"] == 10
    assert w["sent_to_collector_per_sec"] > 0


# ---------------------------------------------------------------- #
#  Аккаунты: 1 ч, ok%, последние ошибки
# ---------------------------------------------------------------- #

def test_per_account_hour_window_and_ok_ratio():
    c, clk = make()
    for _ in range(8):
        c.record_poll("a1", "ok", 200, requests=1, fetched=10, unique=5, state=ACTIVE)
        clk.advance(10)
    c.record_poll("a1", "rate_or_server", 429, state=COOLDOWN)
    row = next(r for r in c.snapshot()["accounts"] if r["name"] == "a1")
    assert row["requests_1h"] == 9
    assert row["ok_ratio_1h"] == pytest.approx(8 / 9, abs=1e-3)
    assert row["unique_comments_1h"] == 40
    assert row["last_error"] == "rate_or_server" and row["last_error_at"] is not None
    assert row["last_ok_at"] is not None
    assert len(row["series"]["unique"]) == m.ACC_SERIES_POINTS
    clk.advance(3700)                                    # через час всё забыто
    row = next(r for r in c.snapshot()["accounts"] if r["name"] == "a1")
    assert row["requests_1h"] == 0 and row["ok_ratio_1h"] is None


# ---------------------------------------------------------------- #
#  Пересчёт состояний и журнал событий
# ---------------------------------------------------------------- #

def _events(c):
    return [(e["account"], e["event"], e["detail"]) for e in c.journal.recent]


def test_state_changes_emit_events():
    c, clk = make()
    c.record_poll("a1", "ok", 200, state=ACTIVE)                      # первое наблюдение — без события
    assert _events(c) == []
    c.record_poll("a1", "auth", 403, state=AUTH_FAILED)
    ev = _events(c)
    assert ("a1", "state_change", "active -> auth_failed (auth_403)") in ev
    assert ("a1", "auth_failed", "auth_403") in ev
    c.record_poll("a1", "auth", 403, state=AUTH_FAILED)                # то же состояние — тишина
    assert len(c.journal.recent) == 2
    c.observe_states({"a1": (BLOCKED_NODE, clk.t)})
    assert any(e[1] == "node_blocked" for e in _events(c))
    c.event("a1", "relogin_ok", "новые cookies")
    assert _events(c)[-1][1] == "relogin_ok"


def test_observe_states_catches_cooldown_expiry():
    c, clk = make()
    c.observe_states({"a1": (COOLDOWN, clk.t)})
    clk.advance(30)
    c.observe_states({"a1": (ACTIVE, clk.t)})
    assert _events(c) == [("a1", "state_change", "cooldown -> active")]


def test_event_journal_writes_jsonl_and_rotates(tmp_path):
    j = m.EventJournal(tmp_path / "events.jsonl", max_bytes=300, backups=2)
    for i in range(30):
        j.emit({"ts": float(i), "account": "a1", "event": "state_change", "detail": "x" * 20, "level": "info"})
        j.flush()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["events.jsonl", "events.jsonl.1", "events.jsonl.2"]
    assert (tmp_path / "events.jsonl").stat().st_size <= 300 + 120
    last = m.read_events(tmp_path / "events.jsonl", limit=3)
    assert last and all(set(r) >= {"ts", "account", "event", "detail"} for r in last)
    assert last[-1]["ts"] == 29.0


def test_read_events_missing_and_garbage(tmp_path):
    assert m.read_events(tmp_path / "nope.jsonl") == []
    p = tmp_path / "e.jsonl"
    p.write_text('{"ts":1,"account":"a","event":"x","detail":""}\nмусор\n', encoding="utf-8")
    assert len(m.read_events(p)) == 1


# ---------------------------------------------------------------- #
#  needs_login, ожидание входа, идентичность
# ---------------------------------------------------------------- #

def test_needs_login_and_waiting_accounts(tmp_path):
    (tmp_path / "cookies").mkdir()
    (tmp_path / "cookies" / "a1.json").write_text("[]", encoding="utf-8")
    (tmp_path / "slots").mkdir()
    (tmp_path / "slots" / "a1.json").write_text(json.dumps({"node": "DE-1", "impersonate": "chrome"}), encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "a1.json").write_text(json.dumps({"country": "DE", "exit_ip": "1.2.3.4"}), encoding="utf-8")
    accounts = [
        {"name": "a1", "enabled": True, "cookie_file": str(tmp_path / "cookies" / "a1.json")},
        {"name": "a2", "enabled": False, "login": True, "cookie_file": str(tmp_path / "cookies" / "a2.json")},
        {"name": "a3", "enabled": True, "cookie_file": str(tmp_path / "cookies" / "a3.json")},
    ]
    c, clk = make(tmp_path, accounts)
    c.refresh_identity()
    c.record_poll("a1", "auth", 401, state=AUTH_FAILED)
    snap = c.snapshot()
    rows = {r["name"]: r for r in snap["accounts"]}
    assert rows["a1"]["needs_login"] and rows["a1"]["needs_login_reason"] == "auth_401"
    assert (rows["a1"]["node"], rows["a1"]["country"], rows["a1"]["exit_ip"]) == ("DE-1", "DE", "1.2.3.4")
    assert rows["a1"]["cookie_age_hours"] is not None and rows["a1"]["cookie_age_hours"] >= 0
    assert rows["a2"]["state"] == m.WAITING_LOGIN and rows["a2"]["needs_login_reason"] == "first_login"
    assert rows["a3"]["state"] == m.WAITING_LOGIN and rows["a3"]["needs_login_reason"] == "no_cookies"
    g = snap["global"]["accounts"]
    assert g["total"] == 3 and g["auth_failed"] == 1 and g["waiting_login"] == 2 and g["needs_login"] == 3


# ---------------------------------------------------------------- #
#  Снимок: валидность, схема, «нет данных»
# ---------------------------------------------------------------- #

def test_snapshot_is_valid_and_json_serializable(tmp_path):
    c, clk = make(tmp_path, [{"name": "a1", "enabled": True}, {"name": "a2", "enabled": False, "login": True}])
    assert m.validate_snapshot(c.snapshot()) == []            # пустой коллектор тоже валиден
    for _ in range(20):
        c.record_poll("a1", "ok", 200, requests=2, fetched=100, unique=30, state=ACTIVE)
        c.record_send(30, 1)
        clk.advance(2)
    snap = json.loads(json.dumps(c.snapshot()))
    assert m.validate_snapshot(snap) == []
    assert snap["schema"] == 1 and len(snap["series"]["unique_comments"]) == m.SERIES_POINTS
    assert any(v for v in snap["series"]["unique_comments"])


def test_validate_snapshot_detects_breakage():
    c, _ = make(accounts=[{"name": "a1", "enabled": True}])
    snap = json.loads(json.dumps(c.snapshot()))
    del snap["global"]["windows"]["1m"]["codes"]["429"]
    snap["accounts"][0]["state"] = "weird"
    snap["schema"] = 2
    errs = m.validate_snapshot(snap)
    assert any("schema" in e for e in errs) and any("codes" in e for e in errs) and any("state" in e for e in errs)
    assert m.validate_snapshot("не json") == ["снимок не объект"]


def test_schema_doc_lists_every_snapshot_field():
    """docs/metrics-schema.md не отстаёт от кода: каждое поле снимка в нём упомянуто."""
    doc = (ROOT / "docs" / "metrics-schema.md").read_text(encoding="utf-8")
    c, _ = make(accounts=[{"name": "a1", "enabled": True}])
    snap = c.snapshot()
    keys = set(snap) | set(snap["global"]) | set(snap["global"]["windows"]["1m"]) | set(snap["global"]["queue"]) \
        | set(snap["global"]["accounts"]) | set(snap["series"]) | set(snap["accounts"][0])
    missing = sorted(k for k in keys if f"`{k}`" not in doc)
    assert not missing, f"в docs/metrics-schema.md нет полей: {missing}"


def test_read_snapshot_missing_or_broken_is_no_data(tmp_path):
    assert m.read_snapshot(tmp_path / "absent.json") is None
    p = tmp_path / "s.json"
    p.write_text("{oops", encoding="utf-8")
    assert m.read_snapshot(p) is None
    p.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    assert m.read_snapshot(p) is None


def test_metrics_failures_do_not_propagate():
    c, _ = make()
    c.record_poll("a1", "ok", status="не число", requests="x", state=ACTIVE)   # мусор на входе
    c.record_send("x", None)
    assert c.errors >= 1                                                       # посчитано, но не брошено


def test_hot_path_is_cheap():
    c, clk = make(accounts=[{"name": f"a{i}", "enabled": True} for i in range(64)])
    t0 = time.perf_counter()
    for i in range(20000):
        c.record_poll(f"a{i % 64}", "ok", 200, requests=2, fetched=100, unique=20, state=ACTIVE)
    per_call = (time.perf_counter() - t0) / 20000
    assert per_call < 200e-6          # на порядки меньше бюджета 1 % CPU при ~3 опросах/с


# ---------------------------------------------------------------- #
#  Запись файла и HTTP
# ---------------------------------------------------------------- #

def test_run_writes_snapshot_file_and_journal(tmp_path):
    c, clk = make(tmp_path, [{"name": "a1", "enabled": True}])
    c.record_poll("a1", "ok", 200, fetched=10, unique=10, state=ACTIVE)
    c.record_poll("a1", "auth", 403, state=AUTH_FAILED)
    snap_path = tmp_path / "metrics_snapshot.json"

    async def body():
        task = asyncio.create_task(c.run(snap_path, interval=0.05, identity_every=0.05))
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(body())
    snap = m.read_snapshot(snap_path)
    assert snap is not None and m.validate_snapshot(snap) == []
    assert not list(tmp_path.glob(".*.tmp"))                    # временные файлы не копятся
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(x)["event"] == "auth_failed" for x in lines)


def test_http_endpoint_serves_snapshot():
    aiohttp = pytest.importorskip("aiohttp")
    c, _ = make(accounts=[{"name": "a1", "enabled": True}])

    async def body():
        srv = m.MetricsServer("127.0.0.1", 0)
        assert await srv.start()
        try:
            url = f"http://127.0.0.1:{srv.bound_port}/metrics.json"
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    assert r.status == 503                      # до первого снимка — честное «нет данных»
                srv.publish(json.dumps(c.snapshot()).encode())
                async with s.get(url) as r:
                    assert r.status == 200
                    data = await r.json()
        finally:
            await srv.stop()
        return data

    data = asyncio.run(body())
    assert m.validate_snapshot(data) == []


def test_http_port_busy_is_not_fatal():
    pytest.importorskip("aiohttp")

    async def body():
        a = m.MetricsServer("127.0.0.1", 0)
        assert await a.start()
        b = m.MetricsServer("127.0.0.1", a.bound_port)
        ok = await b.start()
        await a.stop()
        return ok

    assert asyncio.run(body()) is False


# ---------------------------------------------------------------- #
#  Настоящий account_worker -> метрики (виртуальное время из test_fleet_polling)
# ---------------------------------------------------------------- #

def test_account_worker_feeds_metrics(tmp_path, monkeypatch):
    import random
    pytest.importorskip("curl_cffi")
    pytest.importorskip("aiohttp")
    from test_fleet_polling import make_config, simulate
    from scraper import worker as w
    from scraper.config import ConfigStore
    from scraper.models import FetchResult
    from scraper.scheduler import PollScheduler
    from scraper.state import SeenCache

    cookie = tmp_path / "account_1.json"
    cookie.write_text(json.dumps([{"name": "reddit_session", "value": "x"}]), encoding="utf-8")
    monkeypatch.setattr(w, "load_device_profile", lambda name: None)
    monkeypatch.setattr(w.identity, "read_request", lambda name: None)
    script = {"mode": "401"}

    async def fake_fetch(*a, **kw):
        if script["mode"] == "401":
            return FetchResult(status=401, error_kind="auth")
        return FetchResult(comments=[], status=200, pages_fetched=2)

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
        c = m.MetricsCollector(clock=loop.time, state_dir=tmp_path, journal=m.EventJournal(tmp_path / "ev.jsonl"))
        account = {"name": "account_1", "cookie_file": str(cookie), "proxy_port": 1,
                   "impersonate": "chrome", "user_agent": "UA"}
        c.attach(scheduler=sched, accounts=[dict(account, enabled=True)])
        task = asyncio.create_task(w.account_worker(account, conf, StubSender(), SeenCache(100), sched,
                                                    None, None, metrics=c))
        runner = asyncio.create_task(sched.run())
        await asyncio.sleep(60)
        snap = c.snapshot()
        assert m.validate_snapshot(snap) == []
        row = snap["accounts"][0]
        assert row["state"] == AUTH_FAILED and row["needs_login"] and row["last_error"] == "auth_401"
        assert snap["global"]["windows"]["total"]["codes"]["other"] >= 3
        assert any(e["event"] == "auth_failed" for e in c.journal.recent)

        script["mode"] = "ok"
        await asyncio.sleep(400)
        snap = c.snapshot()
        assert snap["accounts"][0]["state"] == ACTIVE and snap["accounts"][0]["ok_ratio_1h"] > 0
        assert snap["global"]["windows"]["total"]["codes"]["200"] >= 2
        for t in (task, runner):
            t.cancel()
        await asyncio.gather(task, runner, return_exceptions=True)

    simulate(body)
