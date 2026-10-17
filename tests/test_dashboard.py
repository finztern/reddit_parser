"""Модуль 07 — scripts/dashboard.py: раскладка (скриншот-тесты на заглушке данных), сортировка, фильтры,
очередь входа, режим «нет связи», --once/--json, клавиши, пауза и запуск login_hub. Без сети наружу,
без терминала, без реального скрапера (HTTP — локальная заглушка на 127.0.0.1)."""
import http.server
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import dashboard as d  # noqa: E402
from scraper.fleet_polling import DISABLED  # noqa: E402
from scraper.scheduler import PollScheduler, read_runtime_disabled  # noqa: E402

NOW = 1_700_000_000.0
GOLDEN = ROOT / "tests" / "golden" / "dashboard_120x40.txt"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def acc(name, state="active", **kw):
    base = {"name": name, "state": state, "since": NOW - 100, "node": "n1", "country": "NL", "exit_ip": "1.1.1.1",
            "requests_1h": 100, "ok_ratio_1h": 0.99, "last_error": None, "last_error_at": None,
            "last_ok_at": NOW - 5, "unique_comments_1h": 10, "cookie_age_hours": 5.0, "needs_login": False,
            "needs_login_reason": None, "series": {"requests": [1, 2], "ok": [1, 2], "unique": [1, 2]}}
    base.update(kw)
    return base


def snap_with(accounts, **glob):
    counts = {}
    for a in accounts:
        counts[a["state"]] = counts.get(a["state"], 0) + 1
    g = {"windows": {"10s": {}, "1m": {"span_s": 60, "codes": {"200": 100}, "network_errors": 0}},
         "queue": {"size": 10, "max": 100}, "warning": None, "metrics_errors": 0,
         "accounts": {"total": len(accounts), "needs_login": sum(1 for a in accounts if a["needs_login"]), **counts}}
    g.update(glob)
    return {"schema": 1, "ts": NOW, "started_at": NOW - 1000, "uptime_s": 1000.0, "global": g,
            "series": {}, "accounts": accounts}


def render_text(app, size=(120, 40)):
    con = Console(record=True, width=size[0], height=size[1], force_terminal=True, color_system=None,
                  file=open(os.devnull, "w"))
    con.print(app.render(size))
    return con.export_text()


def make_app(data, **kw):
    return d.App(lambda: data, Path("/nonexistent-state"), clock=lambda: NOW, version="test", **kw)


# ---------------------------------------------------------------- #
#  Форматирование и спарклайны
# ---------------------------------------------------------------- #
def test_fmt_age_and_dur():
    assert [d.fmt_age(x) for x in (None, 5, 59, 60, 3599, 3600, 86400 * 2)] == ["—", "5с", "59с", "1м", "59м", "1ч", "2д"]
    assert d.fmt_dur(98765) == "1д 03ч 26м"
    assert d.fmt_dur(125) == "2м 05с"


def test_sparkline_scale_none_and_downsample():
    assert d.sparkline([0, 0, 0], 3) == "▁▁▁"
    assert d.sparkline([0, 1, 2, 3, 4, 5, 6, 7], 8) == "▁▂▃▄▅▆▇█"
    assert d.sparkline([None, 1.0], 2, hi=1.0) == " █"
    assert len(d.sparkline(list(range(180)), 40)) == 40
    assert d.downsample([1, 1, 9, 1], 2, "max") == [1, 9]
    assert d.sparkline([], 10) == ""


def test_short_name_and_acc_num():
    assert d.short_name("account_11") == "#11" and d.short_name(None) == "—"
    assert sorted(["account_10", "account_9", "account_100"], key=d.acc_num) == ["account_9", "account_10", "account_100"]


# ---------------------------------------------------------------- #
#  Статус парка
# ---------------------------------------------------------------- #
def test_fleet_status_levels():
    ok = snap_with([acc("account_1"), acc("account_2")])
    assert d.fleet_status(ok, "live") == ("green", [])
    lvl, why = d.fleet_status(snap_with([acc("account_1"), acc("account_2", "auth_failed", needs_login=True)]), "live")
    assert lvl == "red" and "вне строя" in why[0]    # половина парка вне строя
    big = [acc(f"account_{i}") for i in range(1, 8)] + [acc("account_8", "auth_failed", needs_login=True)]
    lvl, why = d.fleet_status(snap_with(big), "live")
    assert lvl == "yellow" and any("ждут входа" in w for w in why)
    assert d.fleet_status(ok, "stale")[0] == "red"
    assert d.fleet_status(None, "none") == ("red", ["нет данных"])
    assert d.fleet_status(snap_with([acc("account_1", "cooldown")]), "live")[0] == "red"   # active == 0
    q = snap_with([acc("account_1")], queue={"size": 97, "max": 100})
    assert d.fleet_status(q, "live")[0] == "red"
    q = snap_with([acc("account_1")], queue={"size": 85, "max": 100})
    assert d.fleet_status(q, "live")[0] == "yellow"
    bad = snap_with([acc("account_1")], windows={"1m": {"span_s": 60, "codes": {"200": 10, "429": 30}}})
    assert d.fleet_status(bad, "live")[0] == "red"


def test_key_numbers():
    s = snap_with([acc("account_1")], windows={"10s": {"unique_comments_per_sec": 3.0, "sent_to_collector_per_sec": 2.5},
                                               "1m": {"span_s": 60, "codes": {"200": 90, "403": 30}, "network_errors": 30}})
    n = d.key_numbers(s)
    assert n["unique_10s"] == 3.0 and n["sent_10s"] == 2.5 and n["queue_pct"] == 10.0
    assert n["errors_per_min"] == 60.0
    assert d.key_numbers(None)["active"] is None


# ---------------------------------------------------------------- #
#  Сортировка и фильтры
# ---------------------------------------------------------------- #
FLEET = [acc("account_1"), acc("account_2", "cooldown", last_error="ratelimit_low", last_error_at=NOW - 10),
         acc("account_3", "auth_failed", needs_login=True, needs_login_reason="auth_403", ok_ratio_1h=0.2),
         acc("account_4", "disabled"), acc("account_5", "degraded", ok_ratio_1h=0.5),
         acc("account_6", "waiting_login", needs_login=True, needs_login_reason="first_login", ok_ratio_1h=None, requests_1h=0),
         acc("account_7", "blocked_node", ok_ratio_1h=0.9), acc("account_10", requests_1h=500)]


def names(accs):
    return [a["name"] for a in accs]


def test_sort_by_problem_puts_broken_first():
    out = names(d.sort_accounts(FLEET, "problem", NOW))
    assert out[:4] == ["account_3", "account_7", "account_6", "account_5"]
    assert out[-1] in ("account_4", "account_1", "account_10")
    assert d.problem_score(FLEET[2], NOW) > d.problem_score(FLEET[6], NOW) > d.problem_score(FLEET[0], NOW)


def test_sort_modes():
    assert names(d.sort_accounts(FLEET, "name", NOW)) == [f"account_{i}" for i in (1, 2, 3, 4, 5, 6, 7, 10)]
    assert d.sort_accounts(FLEET, "requests", NOW)[0]["name"] == "account_10"
    ok = names(d.sort_accounts(FLEET, "ok", NOW))
    assert ok[0] == "account_3" and ok[-1] == "account_6"       # без запросов — в конец


def test_filters():
    assert names(d.filter_accounts(FLEET, "all")) == names(FLEET)
    assert set(names(d.filter_accounts(FLEET, "problems"))) == {"account_2", "account_3", "account_5", "account_6", "account_7"}
    assert names(d.filter_accounts(FLEET, "auth_failed")) == ["account_3"]
    assert names(d.filter_accounts(FLEET, "disabled")) == ["account_4"]
    assert d.filter_accounts(FLEET, "waiting_login")[0]["name"] == "account_6"


# ---------------------------------------------------------------- #
#  Очередь «перелогинить»
# ---------------------------------------------------------------- #
def queue_snap():
    accs = [acc("account_1"),
            acc("account_2", "auth_failed", needs_login=True, needs_login_reason="auth_403", last_ok_at=NOW - 60),
            acc("account_3", "auth_failed", needs_login=True, needs_login_reason="auth_401", last_ok_at=NOW - 7200),
            acc("account_4", "waiting_login", needs_login=True, needs_login_reason="no_cookies", last_ok_at=None),
            acc("account_5", "waiting_login", needs_login=True, needs_login_reason="first_login", last_ok_at=None),
            acc("account_6", "waiting_login", needs_login=True, needs_login_reason="first_login", last_ok_at=None),
            acc("account_7", "disabled", needs_login=True, needs_login_reason="auth_403")]
    snap = snap_with(accs)
    snap["started_at"], snap["uptime_s"] = NOW - 20000, 20000.0       # last_ok_at всегда внутри аптайма
    return snap


def test_relogin_priority_and_first_login_block():
    queue, first = d.relogin_queue(queue_snap(), NOW)
    # давно не работал > недавно сломался; не работал за весь аптайм = молчит весь аптайм; disabled не в очереди
    assert [q["name"] for q in queue] == ["account_4", "account_3", "account_2"]
    assert [q["priority"] for q in queue] == [1, 2, 3]
    assert queue[0]["never_ok"] and queue[0]["idle_s"] == 20000 and queue[1]["idle_s"] == 7200
    assert [f["name"] for f in first] == ["account_5", "account_6"]
    assert d.relogin_queue(None, NOW) == ([], [])


def test_login_targets_selected_or_first_four_skipping_busy_hub():
    s = queue_snap()
    assert d.login_targets(s, NOW, None, None) == ["account_4", "account_3", "account_2", "account_5"]
    assert d.login_targets(s, NOW, None, "account_2") == ["account_2"]
    hub = {"updated_at": NOW - 1, "sessions": [{"account": "account_4", "status": "waiting_login"},
                                               {"account": "account_9", "status": "done"}]}
    assert d.login_targets(s, NOW, hub, None) == ["account_3", "account_2", "account_5", "account_6"]
    assert d.login_targets(s, NOW, hub, "account_4") == []
    old = {"updated_at": NOW - 999, "sessions": hub["sessions"]}      # хаб давно не пишет — сессий нет
    assert d.hub_sessions(old, NOW) == []


# ---------------------------------------------------------------- #
#  Связь, «нет связи», устойчивость к плохим данным
# ---------------------------------------------------------------- #
def test_link_state_and_offline_banner():
    data = d.Data(snap=snap_with([acc("account_1")]), source="file")
    assert d.link_state(data, NOW + 2)[0] == "live"
    assert d.link_state(data, NOW + 47) == ("stale", 47.0)
    assert d.link_state(d.Data(), NOW)[0] == "none"
    app = d.App(lambda: data, Path("/x"), clock=lambda: NOW + 47, version="t")
    text = render_text(app)
    assert "НЕТ СВЯЗИ" in text and "47с назад" in text
    assert "account_1" in text                              # последние известные данные на месте
    assert len(text.splitlines()) == 40


def test_no_data_does_not_crash():
    text = render_text(make_app(d.Data(error="refused")))
    assert "НЕТ ДАННЫХ" in text and "нет данных" in text and len(text.splitlines()) == 40
    out = d.render_once(d.Data(), NOW, "t")
    assert "НЕТ ДАННЫХ" in out
    assert json.loads(json.dumps(d.summary(d.Data(), NOW)))["link"] == "none"


def test_render_survives_missing_fields():
    s = {"schema": 1, "ts": NOW, "global": {}, "accounts": [{"name": "account_1"}, {"name": "weird", "state": "???"}]}
    text = render_text(make_app(d.Data(snap=s)))
    assert "account_1" in text and "weird" in text
    d.render_once(d.Data(snap=s), NOW, "t")
    d.summary(d.Data(snap=s), NOW)


def test_markup_in_data_is_not_interpreted():
    s = snap_with([acc("account_1", last_error="[red]x[/red]", last_error_at=NOW - 5)])
    ev = [{"ts": NOW, "account": "account_1", "event": "state_change", "detail": "[bold]boom[/bold]", "level": "error"}]
    text = render_text(make_app(d.Data(snap=s, events=ev)))
    assert "[bold]boom" in text


# ---------------------------------------------------------------- #
#  Скриншот-тесты раскладки 120×40 (rich Console(record=True))
# ---------------------------------------------------------------- #
def demo_app(*keys, **kw):
    app = make_app(d.demo_data(NOW), **kw)
    for k in keys:
        app.on_key(k)
    return app


def test_layout_120x40_has_all_seven_sections():
    lines = render_text(demo_app()).splitlines()
    text = "\n".join(lines)
    assert len(lines) == 40 and all(len(ln) <= 120 for ln in lines)
    for marker in ("аптайм", "версия test", "ВНИМАНИЕ",                                 # 1 шапка
                   "Ключевые числа", "В работе", "Уник. комм./с", "В коллектор/с", "Очередь", "Ошибок/мин",   # 2
                   "Графики · 15 мин", "доля 200", "пустые листинги", "ошибки",                              # 3
                   "Аккаунты 55/55", "Состояние", "cook",                                                    # 4
                   "Перелогинить", "ждут первого входа",                                                     # 5
                   "Журнал событий", "auth_failed→active",                                                   # 6
                   "q выход", "p пауза"):                                                                    # 7
        assert marker in text, marker
    assert any(ch in text for ch in d.SPARK)


def test_layout_matches_golden():
    text = render_text(demo_app("down", "down"))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.exists(), "нет эталона: UPDATE_GOLDEN=1 pytest tests/test_dashboard.py"
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_layout_adapts_to_other_sizes():
    for size in ((100, 30), (160, 50), (80, 24)):
        text = render_text(demo_app(), size)
        assert len(text.splitlines()) == size[1]


def test_detail_view_and_footer_messages():
    app = demo_app("down", "d")
    text = render_text(app)
    assert "Деталь · account_" in text and "запросы 15м" in text and "последняя ошибка" in text
    app.on_key("esc")
    assert "Деталь" not in render_text(app)
    app = demo_app("d")                                   # без выбора
    assert "выберите аккаунт" in render_text(app)


def test_table_scrolls_to_selected():
    app = demo_app("s", "s")        # сорт по запросам/ч
    for _ in range(40):
        app.on_key("down")
    assert app.selected is not None
    text = render_text(app)
    assert "▶" + app.selected in text


def test_hub_panel_shows_links_and_password():
    hub = {"updated_at": NOW - 1, "bind": "192.168.0.101", "sessions": [
        {"account": f"account_{i}", "port": 6000 + n, "status": "waiting_login",
         "link": f"http://192.168.0.101:{6000 + n}/vnc.html?autoconnect=1&resize=scale"}
        for n, i in enumerate((46, 47, 48, 45), 1)]}
    data = d.demo_data(NOW)
    data.hub = hub
    app = make_app(data)
    app.hub_password = "Ab3dEfG9"
    text = render_text(app)
    assert "Сессии входа" in text and ":6001/vnc.html" in text and ":6004/vnc.html" in text and "Ab3dEfG9" in text
    assert len(text.splitlines()) == 40
    once = d.render_once(data, NOW, "t")
    assert "vnc.html?autoconnect=1&resize=scale" in once          # в --once ссылки полные


# ---------------------------------------------------------------- #
#  Цвет: NO_COLOR / 16 цветов
# ---------------------------------------------------------------- #
def sgr_params(ansi: str) -> list[int]:
    out = []
    for grp in re.findall(r"\x1b\[([0-9;]*)m", ansi):
        out += [int(x) for x in grp.split(";") if x]
    return out


def ansi_of(renderable, **kw) -> str:
    buf = io.StringIO()
    Console(file=buf, width=120, height=40, force_terminal=True, **kw).print(renderable)
    return buf.getvalue()


def test_no_color_has_no_color_codes_but_keeps_symbols(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    ansi = ansi_of(demo_app().render((120, 40)))
    colors = [p for p in sgr_params(ansi) if 30 <= p <= 37 or 40 <= p <= 47 or 90 <= p <= 107 or p in (38, 48)]
    assert not colors
    assert "✖ auth_failed" in ansi and "▲ degraded" in ansi


def test_color_uses_only_16_color_palette():
    params = sgr_params(ansi_of(demo_app().render((120, 40)), color_system="standard"))
    assert any(31 <= p <= 37 for p in params) and 38 not in params and 48 not in params


# ---------------------------------------------------------------- #
#  --once / --json
# ---------------------------------------------------------------- #
def test_once_is_plain_text():
    out = d.render_once(d.demo_data(NOW), NOW, "t", width=120)
    assert "\x1b" not in out
    for marker in ("Ключевые числа", "Графики", "Аккаунты", "Перелогинить", "Журнал событий", "auth_failed"):
        assert marker in out
    assert max(len(ln) for ln in out.splitlines()) <= 120


def test_json_shape_and_cli(capsys):
    assert d.main(["--json", "--demo"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["link"] == "live" and doc["status"]["level"] in ("green", "yellow", "red")
    assert doc["numbers"]["total"] == 55 and len(doc["accounts"]) == 55
    assert doc["accounts"][0]["problem_score"] >= doc["accounts"][-1]["problem_score"]
    assert doc["relogin_queue"] and doc["relogin_queue"][0]["priority"] == 1
    assert d.main(["--once", "--demo", "--width", "100", "--top", "5"]) == 0
    out = capsys.readouterr().out
    assert "Аккаунты" in out and "\x1b" not in out


def test_once_without_scraper_exits_cleanly(tmp_path, capsys):
    assert d.main(["--once", "--url", "http://127.0.0.1:9/metrics.json", "--state-dir", str(tmp_path)]) == 0
    assert "НЕТ ДАННЫХ" in capsys.readouterr().out


# ---------------------------------------------------------------- #
#  Источники: HTTP, файл, события
# ---------------------------------------------------------------- #
class _Handler(http.server.BaseHTTPRequestHandler):
    body, code = b"", 200

    def do_GET(self):
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def http_server():
    handler = type("H", (_Handler,), {})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{srv.server_address[1]}/metrics.json"
    srv.shutdown()


def test_fetch_prefers_http_then_file_then_none(tmp_path, http_server):
    handler, url = http_server
    handler.body = json.dumps(snap_with([acc("account_1")])).encode()
    snap, src, err = d.fetch_snapshot(url, tmp_path)
    assert src == "http" and snap["accounts"][0]["name"] == "account_1" and not err

    handler.code, handler.body = 503, b'{"error":"no_data_yet"}'
    assert d.fetch_snapshot(url, tmp_path)[0] is None
    (tmp_path / "metrics_snapshot.json").write_text(json.dumps(snap_with([acc("account_2")])))
    snap, src, err = d.fetch_snapshot(url, tmp_path)
    assert src == "file" and snap["accounts"][0]["name"] == "account_2" and "ещё не собрал" in err

    handler.code, handler.body = 200, json.dumps({"schema": 2}).encode()      # чужая схема -> резерв
    assert d.fetch_snapshot(url, tmp_path)[1] == "file"
    handler.body = b"{not json"
    assert d.fetch_snapshot(url, tmp_path)[1] == "file"
    (tmp_path / "metrics_snapshot.json").write_text("{broken")
    assert d.fetch_snapshot(url, tmp_path)[0] is None


def test_fetch_all_keeps_last_known_data_when_scraper_stops(tmp_path):
    url = "http://127.0.0.1:9/metrics.json"        # порт discard: соединение отвергнуто
    prev = d.Data(snap=snap_with([acc("account_1")]), source="http")
    (tmp_path / "events.jsonl").write_text(json.dumps({"ts": NOW, "account": "account_1", "event": "x", "level": "info"}) + "\nмусор\n")
    new = d.fetch_all(url, tmp_path, prev)
    assert new.snap is prev.snap and new.error and new.events[0]["event"] == "x"


def test_fetcher_background_loop_survives_errors():
    calls = []

    def flaky(url, state_dir, prev):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return d.Data(snap=snap_with([acc("account_1")]), source="http")
    f = d.Fetcher("u", Path("/x"), interval=0.01, fetch=flaky).start()
    try:
        deadline = time.time() + 3
        while f.get()[0].snap is None and time.time() < deadline:
            time.sleep(0.01)
        assert f.get()[0].snap is not None and f.get()[1] >= 2
    finally:
        f.stop()


def test_metrics_url_from_config_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("metrics:\n  host: 10.0.0.5\n  port: 9200\n")
    monkeypatch.delenv("METRICS_BIND", raising=False)
    monkeypatch.delenv("METRICS_PORT", raising=False)
    assert d.metrics_url(cfg) == "http://10.0.0.5:9200/metrics.json"
    monkeypatch.setenv("METRICS_PORT", "9300")
    monkeypatch.setenv("METRICS_BIND", "0.0.0.0")
    assert d.metrics_url(cfg) == "http://127.0.0.1:9300/metrics.json"
    assert d.metrics_url(tmp_path / "nope.yaml") == "http://127.0.0.1:9300/metrics.json"


# ---------------------------------------------------------------- #
#  Пауза аккаунта: панель пишет файл — планировщик скрапера применяет
# ---------------------------------------------------------------- #
def test_toggle_pause_roundtrip(tmp_path):
    ok, msg = d.toggle_pause(tmp_path, "account_3", "active")
    assert ok and "пауза" in msg
    assert read_runtime_disabled(tmp_path / "runtime_disabled.json") == {"account_3"}
    assert d.toggle_pause(tmp_path, "account_5", "active")[0]
    assert read_runtime_disabled(tmp_path / "runtime_disabled.json") == {"account_3", "account_5"}
    ok, msg = d.toggle_pause(tmp_path, "account_3", "disabled")      # повтор снимает паузу
    assert ok and "снята" in msg
    assert read_runtime_disabled(tmp_path / "runtime_disabled.json") == {"account_5"}


def test_toggle_pause_refuses_yaml_disabled_and_reports_io_error(tmp_path):
    ok, msg = d.toggle_pause(tmp_path, "account_9", DISABLED)
    assert not ok and "config.yaml" in msg and not (tmp_path / "runtime_disabled.json").exists()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ok, msg = d.toggle_pause(blocker, "account_9", "active")            # state_dir — файл
    assert not ok and "не записать" in msg


def test_read_runtime_disabled_tolerates_garbage(tmp_path):
    p = tmp_path / "r.json"
    assert read_runtime_disabled(p) == set()
    for body, want in (("{bad", set()), ('["a","b"]', {"a", "b"}), ('{"disabled":["x"]}', {"x"}), ('{"disabled":5}', set()),
                       ("42", set())):
        p.write_text(body)
        assert read_runtime_disabled(p) == want


def test_scheduler_applies_runtime_pause_without_yaml(tmp_path):
    path = tmp_path / "runtime_disabled.json"
    conf = {"fleet_polling": {}}
    sched = PollScheduler(conf.get, clock=lambda: 0.0, runtime_disabled_path=path)
    slots = [sched.register(f"account_{i}") for i in (1, 2)]
    sched._sync_disabled(sched.cfg(), 1.0)
    assert all(s.health.state != DISABLED for s in slots)

    d.write_runtime_disabled(path, {"account_2"})
    sched._sync_disabled(sched.cfg(), 2.0)
    assert [s.health.state == DISABLED for s in slots] == [False, True]

    d.write_runtime_disabled(path, set())
    os.utime(path, ns=(10 ** 18, 10 ** 18))                              # mtime точно сменился
    sched._sync_disabled(sched.cfg(), 3.0)
    assert all(s.health.state != DISABLED for s in slots)


def test_scheduler_merges_yaml_and_runtime_disabled(tmp_path):
    path = tmp_path / "runtime_disabled.json"
    d.write_runtime_disabled(path, {"account_2"})
    conf = {"fleet_polling": {"disabled_accounts": ["account_1"]}}
    sched = PollScheduler(conf.get, clock=lambda: 0.0, runtime_disabled_path=path)
    slots = [sched.register(f"account_{i}") for i in (1, 2, 3)]
    sched._sync_disabled(sched.cfg(), 1.0)
    assert [s.health.state == DISABLED for s in slots] == [True, True, False]
    # без пути (старое поведение) — ничего не меняется
    plain = PollScheduler(conf.get, clock=lambda: 0.0)
    s1 = plain.register("account_2")
    plain._sync_disabled(plain.cfg(), 1.0)
    assert s1.health.state != DISABLED


# ---------------------------------------------------------------- #
#  login_hub: запуск из панели
# ---------------------------------------------------------------- #
class FakeProc:
    def __init__(self, rc=None):
        self.rc, self.returncode = rc, rc

    def poll(self):
        return self.rc


class FakePopen:
    def __init__(self, rc=None, text=""):
        self.rc, self.text, self.calls = rc, text, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        kw["stdout"].write(self.text.encode())
        kw["stdout"].flush()
        return FakeProc(self.rc)


def test_hub_launcher_passes_password_via_env_and_protects_log(tmp_path):
    popen = FakePopen()
    hl = d.HubLauncher(tmp_path, script=Path("/x/login_hub.py"), python="py", popen=popen, settle_s=0.15)
    ok, msg = hl.start(["account_3", "account_2"])
    assert ok and "account_3" in msg and hl.password and len(hl.password) == 8
    argv, kw = popen.calls[0]
    assert argv == ["py", "/x/login_hub.py", "--accounts", "account_3", "account_2"]
    assert hl.password not in " ".join(argv)                                # пароль не в argv
    assert kw["env"]["LOGIN_HUB_VNC_PASSWORD"] == hl.password and kw["start_new_session"] is True
    assert (hl.log_path.stat().st_mode & 0o777) == 0o600


def test_hub_launcher_reports_early_exit_and_empty(tmp_path):
    popen = FakePopen(rc=1, text="очередь «нужен вход» пуста\nСвободно 300 МБ RAM — мало\n")
    hl = d.HubLauncher(tmp_path, script=Path("/x"), popen=popen, settle_s=1.0)
    ok, msg = hl.start(["account_3"])
    assert not ok and "код 1" in msg and "Свободно 300 МБ" in msg and hl.password is None
    assert not d.HubLauncher(tmp_path, popen=popen).start([])[0]

    def boom(*a, **k):
        raise OSError("нет python")
    ok, msg = d.HubLauncher(tmp_path, popen=boom).start(["account_1"])
    assert not ok and "нет python" in msg


# ---------------------------------------------------------------- #
#  Клавиши
# ---------------------------------------------------------------- #
def test_parse_keys():
    assert d.parse_keys("q") == ["q"]
    assert d.parse_keys("\x1b[A\x1b[B") == ["up", "down"]
    assert d.parse_keys("\x1bOA") == ["up"]
    assert d.parse_keys("\x1b") == ["esc"]
    assert d.parse_keys("\r") == ["enter"] and d.parse_keys("\x03") == ["ctrl-c"]
    assert d.parse_keys("\x1b[C") == ["other"]


def test_keys_sort_filter_selection_quit_refresh():
    refreshed = []
    app = make_app(d.demo_data(NOW), refresh=lambda: refreshed.append(1))
    assert app.sort == "problem" and app.flt == "all"
    app.on_key("s")
    assert app.sort == "name"
    for _ in range(len(d.SORT_MODES) - 1):
        app.on_key("s")
    assert app.sort == "problem"
    app.on_key("f")
    assert app.flt == "problems"
    assert all(d.is_problem(a) for a in app.visible(app.get_data(), NOW))
    app.on_key("up")                                   # без выбора ↑ -> последний
    last = app.visible(app.get_data(), NOW)[-1]["name"]
    assert app.selected == last
    app.on_key("down")                                 # дальше конца не уходит
    assert app.selected == last
    app.on_key("esc")
    assert app.selected is None and not app.detail
    app.on_key("r")
    assert refreshed == [1]
    assert app.on_key("x") is True
    assert app.on_key("q") is False and app.on_key("ctrl-c") is False


def test_selection_survives_resort_by_name():
    app = demo_app("down", "down")
    chosen = app.selected
    app.on_key("s")
    assert app.selected == chosen


def test_two_presses_open_four_login_sessions():
    """Приёмка: из панели за 2 нажатия (l, y) открываются 4 сессии входа для аккаунтов из очереди."""
    calls = []

    class Launcher:
        password = "Zz9Zz9Zz"

        def start(self, names):
            calls.append(names)
            return True, "ok"
    app = make_app(d.demo_data(NOW), launcher=Launcher(), spawn=lambda fn: fn())
    assert app.on_key("l") is True
    assert "Открыть сессии входа" in render_text(app)       # подтверждение видно в нижней строке
    app.on_key("y")
    queue, first = d.relogin_queue(d.demo_data(NOW).snap, NOW)
    expected = ([q["name"] for q in queue] + [f["name"] for f in first])[:4]
    assert calls == [expected] and len(calls[0]) == 4
    assert app.hub_password == "Zz9Zz9Zz"


def test_login_for_selected_account_and_cancel():
    calls = []

    class Launcher:
        password = None

        def start(self, names):
            calls.append(names)
            return True, "ok"
    app = make_app(d.demo_data(NOW), launcher=Launcher(), spawn=lambda fn: fn())
    app.on_key("down")
    sel = app.selected
    app.on_key("l")
    assert app.confirm == [sel]
    app.on_key("n")                                     # любая другая клавиша — отмена
    assert app.confirm is None and calls == []
    app.on_key("l")
    app.on_key("enter")
    assert calls == [[sel]]


def test_login_refused_when_hub_alive_or_queue_empty_or_no_data():
    data = d.demo_data(NOW)
    data.hub = {"updated_at": NOW - 1, "sessions": [{"account": "account_46", "port": 6001, "status": "waiting_login",
                                                       "link": "http://h:6001/vnc.html"}]}
    app = make_app(data, launcher=object())
    app.on_key("down")
    app.selected = "account_47"
    app.on_key("l")
    assert app.confirm is None and "login_hub уже работает" in app.msg[0]

    healthy = d.Data(snap=snap_with([acc("account_1")]))
    app = make_app(healthy, launcher=object())
    app.on_key("l")
    assert app.confirm is None and "пуста" in app.msg[0]
    app = make_app(d.Data(), launcher=object())
    app.on_key("l")
    assert app.confirm is None and "нет данных" in app.msg[0]


def test_launch_failure_is_reported_in_footer():
    class Launcher:
        password = None

        def start(self, names):
            return False, "login_hub завершился (код 1): мало RAM"
    app = make_app(d.demo_data(NOW), launcher=Launcher(), spawn=lambda fn: fn())
    app.on_key("l")
    app.on_key("y")
    assert "мало RAM" in render_text(app).splitlines()[-1]
    assert app.hub_password is None


def test_pause_key_writes_runtime_file_and_marks_row(tmp_path):
    data = d.demo_data(NOW)
    app = d.App(lambda: data, tmp_path, clock=lambda: NOW, version="t")
    app.on_key("p")
    assert "выберите аккаунт" in app.msg[0]
    app.on_key("down")
    sel = app.selected
    app.on_key("p")
    assert read_runtime_disabled(tmp_path / "runtime_disabled.json") == {sel}
    data.paused = {sel}                                   # fetcher подхватил файл
    assert "⏸" in render_text(app)
    app.on_key("p")
    assert read_runtime_disabled(tmp_path / "runtime_disabled.json") == set()
