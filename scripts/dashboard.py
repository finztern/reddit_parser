#!/usr/bin/env python3
"""
scripts/dashboard.py — панель управления парком в консоли (модуль 07, docs/tz/07-tui-dashboard.md).

Цветная живая панель по ssh/tmux: что с парком, где ошибки, кого перелогинить. Только ЧИТАЕТ метрики
скрапера (модуль 06): `GET /metrics.json` (резерв — state/metrics_snapshot.json) и state/events.jsonl.
К Reddit панель не ходит и на скрапер не влияет, кроме двух явных действий оператора:
  * `l` — запустить scripts/login_hub.py для аккаунтов из очереди «перелогинить»;
  * `p` — поставить/снять паузу аккаунта через state/runtime_disabled.json (без правки yaml);
  * `n` — источник узлов: VLESS+прокси → только VLESS → только прокси (state/node_sources.json,
    аккаунты узлов выключенного источника встают на паузу; см. scripts/node_sources.py).

    venv/bin/python3 scripts/dashboard.py                 # живая панель (лучше 120x40 и больше)
    venv/bin/python3 scripts/dashboard.py --once          # текстовый снимок (для логов и чата)
    venv/bin/python3 scripts/dashboard.py --json          # машинный вывод
    venv/bin/python3 scripts/dashboard.py --demo          # заглушка данных, без скрапера

Клавиши: q выход · r обновить · s сортировка · f фильтр состояний · ↑/↓ выбор аккаунта · l вход ·
d деталь · p пауза · n источник узлов (VLESS/прокси). Esc снимает выбор / закрывает деталь / отменяет подтверждение.

`l`: если аккаунт выбран (↑/↓) — вход для него, иначе — для первых 4 из очереди; затем `y`/Enter
подтверждает (то есть открыть 4 сессии входа = `l`, `y`). Ссылки на noVNC (6001–6004) панель показывает
из state/login_hub/status.json. Пароль VNC передаётся хабу через окружение и показывается в панели.

Цвета: 16/256/truecolor определяет rich; `NO_COLOR` и `--no-color` отключают цвет (состояния
различимы по символам и тексту). Если скрапер остановлен — режим «нет связи» с последними данными.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import select
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402
from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.layout import Layout  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from scraper.fleet_polling import (  # noqa: E402
    ACTIVE, AUTH_FAILED, BLOCKED_NODE, COOLDOWN, DEGRADED, DISABLED,
)
from scraper.identity import STATE_DIR  # noqa: E402
from scraper.metrics import (  # noqa: E402
    DEFAULT_HOST, DEFAULT_PORT, SCHEMA_VERSION, STARTING, WAITING_LOGIN, read_events, read_snapshot,
)
from scraper.scheduler import read_runtime_disabled  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import node_sources as node_src  # noqa: E402

CONFIG_PATH = _ROOT / "config.yaml"
HUB_SCRIPT = _ROOT / "scripts" / "login_hub.py"

# ---------------------------------------------------------------- #
#  Пороги и справочники (всё, что «на глаз», — здесь)
# ---------------------------------------------------------------- #
STALE_AFTER_S = 10.0          # снимок старше (скрапер пишет раз в 2 с) -> «нет связи»
HTTP_TIMEOUT_S = 1.0
EVENT_LINES = 15
HUB_FRESH_S = 15.0            # status.json хаба новее -> хаб жив
LOGIN_BATCH = 4               # сколько сессий входа открывает `l` без выбора

QUEUE_WARN, QUEUE_CRIT = 80.0, 95.0          # очередь пайплайна, %
OK200_WARN, OK200_CRIT = 0.90, 0.70          # доля ответов 200 за минуту
OK_RATIO_WARN, OK_RATIO_CRIT = 0.90, 0.70    # ok% аккаунта за час
BROKEN_RED_SHARE = 0.5                       # столько парка вне строя -> красный
COOKIE_WARN_H = 48.0                         # cookies старше — жёлтый (нет файла — красный)
MIN_REQ_FOR_RATIO = 5                        # меньше запросов за час — ok% не штрафуем

SORT_MODES = ("problem", "name", "requests", "ok")
SORT_LABEL = {"problem": "проблемность", "name": "имя", "requests": "запросы/ч", "ok": "ok%"}
FILTER_MODES = ("all", "problems", ACTIVE, COOLDOWN, DEGRADED, AUTH_FAILED, BLOCKED_NODE, WAITING_LOGIN,
                DISABLED)
FILTER_LABEL = {"all": "все", "problems": "проблемные"}

PROBLEM_STATES = (AUTH_FAILED, BLOCKED_NODE, WAITING_LOGIN, DEGRADED, COOLDOWN)
STATE_SEVERITY = {AUTH_FAILED: 100, BLOCKED_NODE: 90, WAITING_LOGIN: 80, DEGRADED: 60, COOLDOWN: 50,
                  STARTING: 20, DISABLED: 5, ACTIVE: 0}
# состояние -> (символ, стиль бейджа, стиль строки)
STATE_STYLE = {
    ACTIVE: ("●", "black on green", ""),
    COOLDOWN: ("◔", "black on yellow", "yellow"),
    DEGRADED: ("▲", "black on yellow", "yellow"),
    AUTH_FAILED: ("✖", "bold white on red", "red"),
    BLOCKED_NODE: ("■", "bold white on red", "red"),
    WAITING_LOGIN: ("◌", "bold white on magenta", "magenta"),
    STARTING: ("…", "black on cyan", ""),
    DISABLED: ("○", "dim", "dim"),
}
LEVEL_STYLE = {"info": "", "warn": "yellow", "error": "bold red"}
STATUS_CHIP = {  # уровень парка -> (текст, стиль)
    "green": (" ● ПАРК В НОРМЕ ", "bold black on green"),
    "yellow": (" ▲ ВНИМАНИЕ ", "bold black on yellow"),
    "red": (" ✖ ПРОБЛЕМА ", "bold white on red"),
}
SPARK = "▁▂▃▄▅▆▇█"
ERR_SHORT = {"empty_listing": "empty", "ratelimit_low": "ratelim", "rate_or_server": "rate/5xx"}  # узкая колонка
FINAL_HUB_STATUSES = ("done", "failed", "timeout", "cancelled")


# ---------------------------------------------------------------- #
#  Форматирование
# ---------------------------------------------------------------- #
def fmt_age(sec: float | None) -> str:
    """Компактный возраст: 12с / 5м / 3ч / 2д."""
    if sec is None or sec != sec:
        return "—"
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"{int(sec)}с"
    if sec < 3600:
        return f"{int(sec // 60)}м"
    if sec < 86400:
        return f"{int(sec // 3600)}ч"
    return f"{int(sec // 86400)}д"


def fmt_dur(sec: float | None) -> str:
    """Аптайм: 1д 03ч 12м."""
    if sec is None:
        return "—"
    sec = int(max(0, sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}д {h:02d}ч {m:02d}м"
    if h:
        return f"{h}ч {m:02d}м"
    return f"{m}м {sec % 60:02d}с"


def fmt_num(x, nd: int = 1) -> str:
    if x is None:
        return "—"
    if isinstance(x, float) and nd == 0:
        return str(int(round(x)))
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def fmt_pct(ratio) -> str:
    return "—" if ratio is None else f"{ratio * 100:.0f}%"


def fmt_clock(ts: float | None) -> str:
    return "—" if ts is None else time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_cookie_age(h) -> str:
    if h is None:
        return "нет"
    return f"{int(h)}ч" if h < 48 else f"{int(h // 24)}д"


def acc_num(name: str) -> int:
    digits = "".join(c for c in str(name).rsplit("_", 1)[-1] if c.isdigit())
    return int(digits) if digits else 10 ** 9


def short_name(name: str | None) -> str:
    if not name:
        return "—"
    n = acc_num(name)
    return f"#{n}" if n != 10 ** 9 else str(name)


def ratio_style(r, warn: float, crit: float) -> str:
    if r is None:
        return "dim"
    return "green" if r >= warn else ("yellow" if r >= crit else "bold red")


def _clean(values: list) -> list:
    return [v for v in values if isinstance(v, (int, float))]


def downsample(values: list, width: int, agg: str = "mean") -> list:
    """Сжать ряд до `width` столбцов; None не учитываются (пустая корзина -> None)."""
    n = len(values)
    if width <= 0 or n == 0:
        return []
    if n <= width:
        return list(values)
    out = []
    for i in range(width):
        chunk = _clean(values[i * n // width:(i + 1) * n // width])
        out.append(None if not chunk else (max(chunk) if agg == "max" else sum(chunk) / len(chunk)))
    return out


def sparkline(values: list, width: int, hi: float | None = None, agg: str = "mean") -> str:
    """Unicode-спарклайн: масштаб 0..hi (по умолчанию max ряда), None — пробел."""
    vals = downsample(values, width, agg)
    top = hi if hi is not None else max(_clean(vals) or [0.0])
    chars = []
    for v in vals:
        if v is None:
            chars.append(" ")
        elif top <= 0:
            chars.append(SPARK[0])
        else:
            chars.append(SPARK[max(0, min(7, int(round(v / top * 7))))])
    return "".join(chars)


# ---------------------------------------------------------------- #
#  Чистая логика: связь, статус парка, числа, аккаунты, очередь
# ---------------------------------------------------------------- #
@dataclass
class Data:
    snap: dict | None = None        # последний валидный снимок (может быть устаревшим)
    source: str = ""                # http | file | ""
    error: str = ""                 # почему нет свежих данных
    events: list = field(default_factory=list)
    hub: dict | None = None         # state/login_hub/status.json
    paused: set = field(default_factory=set)   # state/runtime_disabled.json


def link_state(data: Data, now: float) -> tuple[str, float | None]:
    """('live'|'stale'|'none', возраст снимка в секундах)."""
    if not data.snap:
        return "none", None
    age = max(0.0, now - float(data.snap.get("ts") or 0))
    return ("live" if age <= STALE_AFTER_S else "stale"), age


def ref_time(data: Data, now: float, link: str) -> float:
    """Опорное «сейчас» для возрастов: при потере связи — момент последнего снимка (данные «замирают»)."""
    return now if link == "live" or not data.snap else float(data.snap.get("ts") or now)


def _g(snap: dict, *path, default=None):
    cur = snap
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def key_numbers(snap: dict | None) -> dict:
    """Ключевые числа шапки панели (окно 1м — стабильнее 10с; 10с — рядом)."""
    if not snap:
        return {k: None for k in ("active", "total", "unique_10s", "unique_1m", "sent_10s", "sent_1m",
                                  "queue_pct", "queue_size", "queue_max", "errors_per_min")}
    acc = _g(snap, "global", "accounts", default={})
    w10, w1 = _g(snap, "global", "windows", "10s", default={}), _g(snap, "global", "windows", "1m", default={})
    q = _g(snap, "global", "queue", default={})
    errors = None
    span = w1.get("span_s")
    if span:
        codes = w1.get("codes") or {}
        bad = sum(v for k, v in codes.items() if k != "200") + (w1.get("network_errors") or 0)
        errors = bad / span * 60.0
    size, mx = q.get("size"), q.get("max")
    return {
        "active": acc.get("active"), "total": acc.get("total"),
        "unique_10s": w10.get("unique_comments_per_sec"), "unique_1m": w1.get("unique_comments_per_sec"),
        "sent_10s": w10.get("sent_to_collector_per_sec"), "sent_1m": w1.get("sent_to_collector_per_sec"),
        "queue_pct": (size / mx * 100.0) if size is not None and mx else None,
        "queue_size": size, "queue_max": mx, "errors_per_min": errors,
    }


def fleet_status(snap: dict | None, link: str) -> tuple[str, list[str]]:
    """Цвет парка и причины: ('green'|'yellow'|'red', [текст, ...])."""
    if not snap:
        return "red", ["нет данных"]
    rank = {"green": 0, "yellow": 1, "red": 2}
    level, reasons = "green", []

    def bump(lvl: str, why: str) -> None:
        nonlocal level
        if rank[lvl] > rank[level]:
            level = lvl
        reasons.append(why)

    if link != "live":
        bump("red", "нет связи со скрапером")
    acc = _g(snap, "global", "accounts", default={})
    total = acc.get("total") or 0
    broken = (acc.get("auth_failed") or 0) + (acc.get("blocked_node") or 0) + (acc.get("waiting_login") or 0)
    if total and not acc.get("active"):
        bump("red", "нет активных аккаунтов")
    elif total and broken / total >= BROKEN_RED_SHARE:
        bump("red", f"вне строя {broken} из {total}")
    elif broken:
        bump("yellow", f"вне строя: {broken}")
    if acc.get("needs_login"):
        bump("yellow", f"ждут входа: {acc['needs_login']}")
    if _g(snap, "global", "warning"):
        bump("yellow", f"планировщик: {_g(snap, 'global', 'warning')}")
    nums = key_numbers(snap)
    qp = nums["queue_pct"]
    if qp is not None and qp >= QUEUE_CRIT:
        bump("red", f"очередь {qp:.0f}%")
    elif (qp is not None and qp >= QUEUE_WARN) or _g(snap, "global", "queue", "degraded"):
        bump("yellow", "очередь коллектора перегружена")
    codes = _g(snap, "global", "windows", "1m", "codes", default={})
    n = sum(codes.values()) if codes else 0
    if n >= 20:
        r = codes.get("200", 0) / n
        if r < OK200_CRIT:
            bump("red", f"ответов 200 всего {r * 100:.0f}%")
        elif r < OK200_WARN:
            bump("yellow", f"ответов 200 {r * 100:.0f}%")
    if _g(snap, "global", "metrics_errors", default=0):
        bump("yellow", "сбои сбора метрик")
    return level, reasons


def is_problem(a: dict) -> bool:
    return a.get("state") in PROBLEM_STATES or bool(a.get("needs_login"))


def problem_score(a: dict, now: float) -> int:
    score = STATE_SEVERITY.get(a.get("state"), 30)
    if a.get("needs_login"):
        score = max(score, 80)
    ok, req = a.get("ok_ratio_1h"), a.get("requests_1h") or 0
    if ok is not None and req >= MIN_REQ_FOR_RATIO:
        score += int(round((1.0 - ok) * 30))
    le = a.get("last_error_at")
    if le and now - le < 300:
        score += 10
    return score


def sort_accounts(accs: list[dict], mode: str, now: float) -> list[dict]:
    if mode == "name":
        return sorted(accs, key=lambda a: acc_num(a["name"]))
    if mode == "requests":
        return sorted(accs, key=lambda a: (-(a.get("requests_1h") or 0), acc_num(a["name"])))
    if mode == "ok":   # худшие сверху; без запросов — в конец
        return sorted(accs, key=lambda a: (a.get("ok_ratio_1h") is None,
                                           a.get("ok_ratio_1h") if a.get("ok_ratio_1h") is not None else 0,
                                           acc_num(a["name"])))
    return sorted(accs, key=lambda a: (-problem_score(a, now), acc_num(a["name"])))


def filter_accounts(accs: list[dict], mode: str) -> list[dict]:
    if mode == "all":
        return list(accs)
    if mode == "problems":
        return [a for a in accs if is_problem(a)]
    return [a for a in accs if a.get("state") == mode]


def relogin_queue(snap: dict | None, now: float) -> tuple[list[dict], list[dict]]:
    """(очередь «перелогинить», блок «ждут первого входа»).

    Приоритет очереди: давно не работал > недавно сломался (время с last_ok_at; не работал ни разу —
    молчит весь аптайм скрапера). Новые без первого входа (`first_login`) — отдельный блок, по номеру."""
    if not snap:
        return [], []
    started = float(snap.get("started_at") or now)
    queue, first = [], []
    for a in snap.get("accounts") or []:
        if not a.get("needs_login") or a.get("state") == DISABLED:
            continue
        reason = a.get("needs_login_reason")
        if reason == "first_login":
            first.append({"name": a["name"], "state": a.get("state"), "reason": reason,
                          "node": a.get("node"), "country": a.get("country")})
            continue
        last_ok = a.get("last_ok_at")
        idle = now - (last_ok if last_ok else started)
        queue.append({"name": a["name"], "state": a.get("state"), "reason": reason or a.get("last_error"),
                      "idle_s": max(0.0, idle), "never_ok": not last_ok, "node": a.get("node"),
                      "country": a.get("country")})
    queue.sort(key=lambda r: (-r["idle_s"], acc_num(r["name"])))
    first.sort(key=lambda r: acc_num(r["name"]))
    for i, r in enumerate(queue, 1):
        r["priority"] = i
    return queue, first


def hub_sessions(hub: dict | None, now: float) -> list[dict]:
    """Живые сессии login_hub (status.json свежий и статус не финальный)."""
    if not hub or now - float(hub.get("updated_at") or 0) > HUB_FRESH_S:
        return []
    return [s for s in hub.get("sessions") or [] if s.get("status") not in FINAL_HUB_STATUSES]


def login_targets(snap: dict | None, now: float, hub: dict | None, selected: str | None,
                  limit: int = LOGIN_BATCH) -> list[str]:
    """Кого открывать по `l`: выбранный аккаунт либо первые `limit` из очереди (затем из «первого входа»)."""
    busy = {s.get("account") for s in hub_sessions(hub, now)}
    if selected:
        return [] if selected in busy else [selected]
    queue, first = relogin_queue(snap, now)
    names = [r["name"] for r in queue] + [r["name"] for r in first]
    return [n for n in names if n not in busy][:limit]


def summary(data: Data, now: float, sort: str = "problem", flt: str = "all") -> dict:
    """Машинный вывод (--json) — тот же расчёт, что у панели."""
    link, age = link_state(data, now)
    ref = ref_time(data, now, link)
    snap = data.snap
    level, reasons = fleet_status(snap, link)
    accs = sort_accounts(filter_accounts((snap or {}).get("accounts") or [], flt), sort, ref)
    queue, first = relogin_queue(snap, ref)
    return {
        "ts": now, "link": link, "snapshot_age_s": age, "source": data.source, "error": data.error or None,
        "status": {"level": level if link != "none" else "red", "reasons": reasons},
        "uptime_s": (snap or {}).get("uptime_s"),
        "numbers": key_numbers(snap),
        "accounts": [{**{k: a.get(k) for k in ("name", "state", "since", "country", "node", "requests_1h",
                                               "ok_ratio_1h", "last_error", "last_error_at",
                                               "cookie_age_hours", "needs_login", "needs_login_reason")},
                      "paused": a["name"] in data.paused, "problem_score": problem_score(a, ref)}
                     for a in accs],
        "relogin_queue": queue, "waiting_first_login": first,
        "login_hub": hub_sessions(data.hub, now), "paused": sorted(data.paused),
        "events": data.events,
    }


# ---------------------------------------------------------------- #
#  Источники данных
# ---------------------------------------------------------------- #
def metrics_url(config_path: Path = CONFIG_PATH) -> str:
    """Тот же адрес, что слушает скрапер: METRICS_BIND/METRICS_PORT > config.yaml `metrics:` > 127.0.0.1:9100."""
    try:
        cfg = (yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}).get("metrics") or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    host = os.environ.get("METRICS_BIND") or str(cfg.get("host", DEFAULT_HOST))
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = os.environ.get("METRICS_PORT") or cfg.get("port", DEFAULT_PORT)
    return f"http://{host}:{int(port)}/metrics.json"


def _read_json_file(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fetch_snapshot(url: str, state_dir: Path, timeout: float = HTTP_TIMEOUT_S) -> tuple[dict | None, str, str]:
    """(снимок | None, источник, причина). HTTP, затем резервный файл; ничего не бросает."""
    err = ""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("schema") == SCHEMA_VERSION:
            return data, "http", ""
        err = "чужая схема метрик"
    except urllib.error.HTTPError as e:
        err = "скрапер ещё не собрал снимок" if e.code == 503 else f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        err = f"{url}: {getattr(e, 'reason', e)}"
    snap = read_snapshot(state_dir / "metrics_snapshot.json")
    if snap is not None:
        return snap, "file", err
    return None, "", err or "нет данных"


def fetch_all(url: str, state_dir: Path, prev: Data | None = None, event_lines: int = EVENT_LINES) -> Data:
    snap, source, err = fetch_snapshot(url, state_dir)
    if snap is None and prev is not None and prev.snap is not None:
        snap, source = prev.snap, prev.source   # «нет связи»: держим последние известные данные
    return Data(snap=snap, source=source, error=err,
                events=read_events(state_dir / "events.jsonl", event_lines),
                hub=_read_json_file(state_dir / "login_hub" / "status.json"),
                paused=read_runtime_disabled(state_dir / "runtime_disabled.json"))


class Fetcher:
    """Фоновое обновление раз в `interval` секунд: интерфейс не ждёт сеть."""

    def __init__(self, url: str, state_dir: Path, interval: float = 1.5,
                 fetch: Callable[..., Data] = fetch_all):
        self.url, self.state_dir, self.interval, self._fetch = url, state_dir, interval, fetch
        self.data, self.version = Data(), 0
        self._lock, self._wake, self._stop = threading.Lock(), threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                new = self._fetch(self.url, self.state_dir, self.data)
            except Exception as e:  # панель не должна падать из-за сбоя чтения
                new = Data(snap=self.data.snap, source=self.data.source, error=f"сбой чтения: {e}",
                           events=self.data.events, hub=self.data.hub, paused=self.data.paused)
            with self._lock:
                self.data = new
                self.version += 1
            self._wake.wait(self.interval)
            self._wake.clear()

    def start(self) -> "Fetcher":
        self._thread.start()
        return self

    def refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def get(self) -> tuple[Data, int]:
        with self._lock:
            return self.data, self.version


# ---------------------------------------------------------------- #
#  Действия оператора: пауза аккаунта, запуск login_hub
# ---------------------------------------------------------------- #
def write_runtime_disabled(path: Path, names: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"disabled": sorted(names), "updated_at": int(time.time())},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def toggle_pause(state_dir: Path, name: str, snap_state: str | None) -> tuple[bool, str]:
    """Пауза/снятие паузы. (успех, сообщение). Выключенный через config.yaml панель не включает."""
    path = state_dir / "runtime_disabled.json"
    names = read_runtime_disabled(path)
    if name in names:
        names.discard(name)
        msg = f"{name}: пауза снята"
    else:
        if snap_state == DISABLED:
            return False, f"{name} выключен в config.yaml (fleet_polling.disabled_accounts) — панель его не включает"
        names.add(name)
        msg = f"{name}: пауза (disabled в рантайме)"
    try:
        write_runtime_disabled(path, names)
    except OSError as e:
        return False, f"не записать {path}: {e}"
    return True, msg


def gen_vnc_password(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


class HubLauncher:
    """Запуск login_hub.py отдельным процессом (переживает панель; сессии гаснут по своему таймауту)."""

    def __init__(self, state_dir: Path, script: Path = HUB_SCRIPT, python: str = sys.executable,
                 popen: Callable = subprocess.Popen, settle_s: float = 2.0):
        self.state_dir, self.script, self.python = state_dir, script, python
        self._popen, self.settle_s = popen, settle_s
        self.password: str | None = None

    @property
    def log_path(self) -> Path:
        return self.state_dir / "login_hub" / "dashboard-hub.log"

    def start(self, names: list[str]) -> tuple[bool, str]:
        if not names:
            return False, "некого запускать: очередь пуста"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        pw = gen_vnc_password()
        env = {**os.environ, "LOGIN_HUB_VNC_PASSWORD": pw}   # не в argv
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # в логе хаба есть пароль VNC
        with os.fdopen(fd, "wb") as log:
            try:
                proc = self._popen([self.python, str(self.script), "--accounts", *names], stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                                   cwd=str(_ROOT))
            except OSError as e:
                return False, f"не запустить login_hub: {e}"
        deadline = time.time() + self.settle_s
        while time.time() < deadline:
            if proc.poll() is not None:   # хаб сразу вышел: RAM, зависимости, bind, пустая очередь...
                return False, f"login_hub завершился (код {proc.returncode}): {self._log_tail()}"
            time.sleep(0.1)
        self.password = pw
        return True, f"login_hub запущен для {', '.join(names)}"

    def _log_tail(self) -> str:
        try:
            lines = [ln.strip() for ln in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip()]
        except OSError:
            return "лога нет"
        return (lines[-1] if lines else "без вывода")[:160]


# ---------------------------------------------------------------- #
#  Отрисовка (панели принимают данные и размеры — без ввода-вывода)
# ---------------------------------------------------------------- #
def badge(state: str | None, paused: bool = False) -> Text:
    """Бейдж состояния; поставленный с панели на паузу аккаунт помечен ⏸ (до и после применения скрапером)."""
    sym, style, _ = STATE_STYLE.get(state or "", ("?", "", ""))
    if paused:
        sym = "⏸"
    return Text(f"{sym} {state or '?'}", style=style)


def render_header(data: Data, now: float, version: str, link: str, age: float | None) -> Panel:
    snap, ref = data.snap, ref_time(data, now, link)
    level, reasons = fleet_status(snap, link)
    left = Text()
    left.append(time.strftime("%H:%M:%S", time.localtime(now)), style="bold")
    left.append(f"   аптайм {fmt_dur((snap or {}).get('uptime_s'))}   версия {version}   схема {SCHEMA_VERSION}")
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    right = Text()
    if link != "live":
        if snap:
            right.append(f" НЕТ СВЯЗИ · данные от {fmt_clock(snap.get('ts'))} ({fmt_age(age)} назад) ",
                         style="bold white on red")
        else:
            right.append(" НЕТ ДАННЫХ ", style="bold white on red")
        right.append(" ")
    elif reasons:
        right.append(reasons[0] + "  ", style="yellow" if level == "yellow" else "bold red")
    chip_text, chip_style = STATUS_CHIP[level if link != "none" else "red"]
    if link == "live" or snap:
        right.append(chip_text, style=chip_style)
    grid.add_row(left, right)
    return Panel(grid, box=box.ROUNDED, padding=(0, 1))


def render_numbers(snap: dict | None, width: int | None = None) -> Panel:
    n = key_numbers(snap)
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True, style="dim")

    def row(label: str, value: Text, extra: str = "") -> None:
        grid.add_row(label, value, extra)

    active, total = n["active"], n["total"]
    st = "dim" if active is None else ("bold red" if not active else ("bold green" if active == total else "bold yellow"))
    row("В работе", Text(f"{fmt_num(active, 0)} / {fmt_num(total, 0)}", style=st))
    row("Уник. комм./с", Text(fmt_num(n["unique_10s"]), style="bold cyan"), f"1м {fmt_num(n['unique_1m'])}")
    row("В коллектор/с", Text(fmt_num(n["sent_10s"]), style="bold cyan"), f"1м {fmt_num(n['sent_1m'])}")
    qp = n["queue_pct"]
    qs = "dim" if qp is None else ("bold red" if qp >= QUEUE_CRIT else "bold yellow" if qp >= QUEUE_WARN else "bold green")
    row("Очередь", Text("—" if qp is None else f"{qp:.0f}%", style=qs),
        "" if n["queue_size"] is None else f"{n['queue_size']}/{n['queue_max']}")
    e = n["errors_per_min"]
    row("Ошибок/мин", Text(fmt_num(e), style="dim" if e is None else ("bold green" if e < 1 else "bold yellow" if e < 10 else "bold red")))
    return Panel(grid, title="Ключевые числа", title_align="left", box=box.ROUNDED, padding=(0, 1), width=width)


CHART_ROWS = (  # (подпись, ключ ряда, формат последнего значения, агрегат, масштаб)
    ("уник. комм./с", "unique_comments", "num", "mean", None),
    ("отправка/с", "sent_to_collector", "num", "mean", None),
    ("доля 200", "ok200_ratio", "pct", "mean", 1.0),
    ("пустые листинги", "empty_listings", "int", "max", None),
    ("ошибки", "errors", "int", "max", None),
)


def render_charts(snap: dict | None, width: int) -> Panel:
    series = (snap or {}).get("series") or {}
    label_w, val_w = 15, 6
    spark_w = max(10, width - 4 - label_w - val_w - 2)
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=label_w, no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(width=val_w, justify="right", no_wrap=True)
    for label, key, fmt, agg, scale in CHART_ROWS:
        pts = list(series.get(key) or [])[:-1]       # последняя точка — текущая неполная
        last = next((v for v in reversed(pts) if v is not None), None)
        if fmt == "pct":
            val, style = fmt_pct(last), ratio_style(last, OK200_WARN, OK200_CRIT)
        else:
            val = "—" if last is None else (f"{last:.1f}" if fmt == "num" else str(int(last)))
            style = "cyan" if key in ("unique_comments", "sent_to_collector") else (
                "dim" if not last else ("yellow" if key == "empty_listings" else "red"))
        grid.add_row(label, Text(sparkline(pts, spark_w, scale, agg) or "нет данных", style=style),
                     Text(val, style=style))
    return Panel(grid, title="Графики · 15 мин", title_align="left", box=box.ROUNDED, padding=(0, 1))


TABLE_FIXED_W = 13 + 16 + 12 + 7 + 5 + 5    # суммарная ширина колонок, кроме «Ошибка» (rich включает зазор)


def render_table(accs: list[dict], data: Data, ref: float, selected: str | None, rows: int, sort: str,
                 flt: str, total: int, width: int = 76) -> Panel:
    """`width` — полная ширина панели (с рамкой): колонка «Ошибка» забирает остаток."""
    t = Table(box=box.SIMPLE_HEAD, expand=False, padding=(0, 1), collapse_padding=True, pad_edge=False,
              show_edge=False)
    # ширина колонки у rich включает зазор (1): отсюда «содержимое + 1»; всего должно влезать в 70 колонок
    t.add_column("Аккаунт", no_wrap=True, width=13)
    t.add_column("Состояние", no_wrap=True, width=16, overflow="ellipsis")
    t.add_column("Нода", no_wrap=True, overflow="ellipsis", width=12)
    t.add_column("зап/ч", justify="right", no_wrap=True, width=7)
    t.add_column("ok%", justify="right", no_wrap=True, width=5)
    t.add_column("Ошибка", no_wrap=True, overflow="ellipsis", width=max(8, width - 4 - TABLE_FIXED_W))
    t.add_column("cook", justify="right", no_wrap=True, width=5)
    sel_idx = next((i for i, a in enumerate(accs) if a["name"] == selected), None)
    rows = max(1, rows)
    start = 0
    if len(accs) > rows:
        start = max(0, min(len(accs) - rows, (sel_idx or 0) - rows // 2))
    shown = accs[start:start + rows]
    for a in shown:
        st = a.get("state")
        row_style = STATE_STYLE.get(st, ("", "", ""))[2]
        mark = "▶" if a["name"] == selected else ""
        geo = " ".join(x for x in (a.get("country"), a.get("node")) if x) or "—"
        err = "—"
        if a.get("last_error"):
            le = ERR_SHORT.get(a["last_error"], a["last_error"])
            err = f"{le} {fmt_age(ref - a['last_error_at'])}" if a.get("last_error_at") else le
        ck = a.get("cookie_age_hours")
        ck_style = "bold red" if ck is None else ("yellow" if ck >= COOKIE_WARN_H else "dim")
        ok = a.get("ok_ratio_1h")
        t.add_row(Text(mark + a["name"], style=row_style), badge(st, a["name"] in data.paused),
                  Text(geo, style=row_style), Text(str(a.get("requests_1h") if a.get("requests_1h") is not None else "—")),
                  Text(fmt_pct(ok), style=ratio_style(ok, OK_RATIO_WARN, OK_RATIO_CRIT)),
                  Text(err, style="red" if err != "—" and st in (AUTH_FAILED, BLOCKED_NODE) else row_style),
                  Text(fmt_cookie_age(ck), style=ck_style),
                  style=Style(reverse=True) if a["name"] == selected else None)
    title = f"Аккаунты {len(accs)}/{total} · сорт: {SORT_LABEL[sort]} · фильтр: {FILTER_LABEL.get(flt, flt)}"
    body: Text | Group = t
    if not accs:
        body = Text("нет данных" if not data.snap else "нет аккаунтов под фильтр", style="dim")
    elif len(accs) > rows:
        body = Group(t, Text(f"  {start + 1}–{start + len(shown)} из {len(accs)}", style="dim"))
    return Panel(body, title=title, title_align="left", box=box.ROUNDED, padding=(0, 1))


def render_relogin(queue: list[dict], first: list[dict], width: int, lines: int, ref: float) -> Panel:
    out: list[Text] = []
    if not queue and not first:
        out.append(Text("очередь пуста — входить некуда", style="green"))
    for r in queue:
        t = Text()
        t.append(f"{r['priority']:>2} ", style="bold")
        t.append(f"{r['name']:<10} ", style="red" if r["state"] == AUTH_FAILED else "")
        t.append(f"{(r['reason'] or '—'):<10} ")
        t.append("не работал" if r["never_ok"] else f"молчит {fmt_age(r['idle_s'])}", style="dim")
        out.append(t)
    if first:
        out.append(Text(f"ждут первого входа ({len(first)}):", style="bold magenta"))
        names = ", ".join(short_name(r["name"]) for r in first)
        out.append(Text(names, style="magenta"))
    keep = max(1, lines)
    if len(out) > keep:
        hidden = len(out) - (keep - 1)
        out = out[:keep - 1] + [Text(f"… ещё {hidden}", style="dim")]
    title = f"Перелогинить · очередь {len(queue)}" + (f" · новых {len(first)}" if first else "")
    return Panel(Group(*out), title=title, title_align="left", box=box.ROUNDED, padding=(0, 1))


def event_text(rec: dict) -> Text:
    ev, detail = rec.get("event") or "", rec.get("detail") or ""
    msg = detail if ev == "state_change" and detail else (f"{ev}: {detail}" if detail else ev)
    msg = msg.replace(" -> ", "→")
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(fmt_clock(rec.get("ts")) + " ", style="dim")
    t.append(f"{short_name(rec.get('account')):<4}", style="bold")
    t.append(msg, style=LEVEL_STYLE.get(rec.get("level") or "info", ""))
    return t


def render_log(events: list[dict], lines: int = EVENT_LINES) -> Panel:
    rows = [event_text(r) for r in events[-lines:]]
    body = Group(*rows) if rows else Text("событий пока нет", style="dim")
    return Panel(body, title="Журнал событий", title_align="left", box=box.ROUNDED, padding=(0, 1))


def hub_panel_lines(sessions: list[dict], password: str | None) -> list[Text]:
    out = []
    for s in sessions:
        link = s.get("link") or ""
        host_link = link.split("?", 1)[0]
        t = Text()
        t.append(f"{s.get('account', '?'):<10} ", style="bold")
        t.append(f"{s.get('status', '?'):<12} ", style="yellow" if s.get("status") == "waiting_user" else "")
        t.append(host_link or f":{s.get('port')}", style=Style(link=link) if link else "cyan")
        out.append(t)
    pw = Text("VNC-пароль: ", style="dim")
    pw.append(password or "в state/login_hub/dashboard-hub.log", style="bold")
    out.append(pw)
    return out


def render_hub(sessions: list[dict], password: str | None) -> Panel:
    return Panel(Group(*hub_panel_lines(sessions, password)), title="Сессии входа (login_hub, noVNC)",
                 title_align="left", box=box.ROUNDED, padding=(0, 1), border_style="magenta")


def render_detail(a: dict, data: Data, ref: float, events: list[dict]) -> Panel:
    st = a.get("state")
    g = Table.grid(padding=(0, 2))
    g.add_column(style="dim", no_wrap=True)
    g.add_column()

    def row(k: str, v) -> None:
        g.add_row(k, v if isinstance(v, Text) else Text(str(v)))

    row("состояние", badge(st, a["name"] in data.paused))
    row("в состоянии", f"{fmt_age(ref - a['since'])} (с {fmt_clock(a['since'])})" if a.get("since") else "—")
    row("нода", a.get("node") or "—")
    row("страна / IP", f"{a.get('country') or '—'} / {a.get('exit_ip') or '—'}")
    row("запросов за час", a.get("requests_1h") if a.get("requests_1h") is not None else "—")
    row("ok% за час", Text(fmt_pct(a.get("ok_ratio_1h")), style=ratio_style(a.get("ok_ratio_1h"), OK_RATIO_WARN, OK_RATIO_CRIT)))
    row("уник. комм. за час", a.get("unique_comments_1h") if a.get("unique_comments_1h") is not None else "—")
    row("последний успех", f"{fmt_age(ref - a['last_ok_at'])} назад" if a.get("last_ok_at") else "—")
    err = "—"
    if a.get("last_error"):
        err = a["last_error"] + (f" · {fmt_age(ref - a['last_error_at'])} назад" if a.get("last_error_at") else "")
    row("последняя ошибка", Text(err, style="red" if err != "—" else ""))
    ck = a.get("cookie_age_hours")
    row("cookies", Text("нет файла" if ck is None else f"{ck:.0f} ч", style="bold red" if ck is None else ""))
    row("нужен вход", f"да ({a.get('needs_login_reason')})" if a.get("needs_login") else "нет")
    row("пауза панели", "да" if a["name"] in data.paused else "нет")
    ser = a.get("series") or {}
    for label, key in (("запросы 15м", "requests"), ("ok 15м", "ok"), ("уник. 15м", "unique")):
        row(label, Text(sparkline(list(ser.get(key) or []), 30) or "—", style="cyan"))
    mine = [e for e in events if e.get("account") == a["name"]][-8:]
    body = [g, Text(""), Text("События аккаунта:", style="bold")]
    body += [event_text(e) for e in mine] or [Text("нет событий", style="dim")]
    return Panel(Group(*body), title=f"Деталь · {a['name']}  (d/Esc — закрыть)", title_align="left",
                 box=box.ROUNDED, padding=(0, 1))


# ---------------------------------------------------------------- #
#  Приложение: состояние, клавиши, раскладка
# ---------------------------------------------------------------- #
def git_version(root: Path = _ROOT) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "describe", "--always", "--dirty", "--tags"],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


class App:
    """Состояние панели. Данные подаёт `get_data`; время — `clock` (в тестах фиксировано)."""

    def __init__(self, get_data: Callable[[], Data], state_dir: Path, launcher: HubLauncher | None = None,
                 clock: Callable[[], float] = time.time, version: str = "?", refresh: Callable[[], None] = lambda: None,
                 spawn: Callable[[Callable], None] | None = None):
        self.get_data, self.state_dir, self.launcher = get_data, state_dir, launcher
        self.clock, self.version, self._refresh = clock, version, refresh
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.sort_i = self.filter_i = 0
        self.selected: str | None = None
        self.detail = False
        self.detail_events: list[dict] = []
        self.confirm: list[str] | None = None
        self.msg: tuple[str, str, float] | None = None
        self.hub_password: str | None = None

    # --- вспомогательное ---
    @property
    def sort(self) -> str:
        return SORT_MODES[self.sort_i]

    @property
    def flt(self) -> str:
        return FILTER_MODES[self.filter_i]

    def say(self, text: str, style: str = "bold", secs: float = 6.0) -> None:
        self.msg = (text, style, self.clock() + secs)

    def visible(self, data: Data, ref: float) -> list[dict]:
        accs = filter_accounts((data.snap or {}).get("accounts") or [], self.flt)
        return sort_accounts(accs, self.sort, ref)

    def _ctx(self) -> tuple[Data, float, str, float | None]:
        data, now = self.get_data(), self.clock()
        link, age = link_state(data, now)
        return data, ref_time(data, now, link), link, age

    # --- клавиши: False = выход ---
    def on_key(self, key: str) -> bool:
        data, ref, link, _ = self._ctx()
        if self.confirm is not None:
            names, self.confirm = self.confirm, None
            if key in ("y", "Y", "enter"):
                self._start_login(names)
            else:
                self.say("вход отменён", "dim", 3)
            return True
        if key in ("q", "Q", "ctrl-c"):
            return False
        if key == "esc":
            self.selected, self.detail = None, False
        elif key in ("r", "R"):
            self._refresh()
            self.say("обновляю…", "dim", 2)
        elif key in ("s", "S"):
            self.sort_i = (self.sort_i + 1) % len(SORT_MODES)
        elif key in ("f", "F"):
            self.filter_i = (self.filter_i + 1) % len(FILTER_MODES)
        elif key in ("up", "down"):
            vis = self.visible(data, ref)
            names = [a["name"] for a in vis]
            if names:
                i = names.index(self.selected) if self.selected in names else None
                step = -1 if key == "up" else 1
                self.selected = names[(len(names) - 1 if key == "up" else 0) if i is None else max(0, min(len(names) - 1, i + step))]
        elif key in ("d", "D"):
            self._toggle_detail(data)
        elif key in ("p", "P"):
            self._pause(data)
        elif key in ("n", "N"):
            self._cycle_sources()
        elif key in ("l", "L"):
            self._ask_login(data, ref)
        return True

    def _toggle_detail(self, data: Data) -> None:
        if self.detail:
            self.detail = False
        elif self.selected:
            self.detail = True
            self.detail_events = read_events(self.state_dir / "events.jsonl", 300)
        else:
            self.say("выберите аккаунт стрелками ↑/↓", "yellow", 3)

    def _cycle_sources(self) -> None:
        """Источник узлов: оба -> только VLESS -> только прокси. Паузы аккаунтов пересобираются сразу."""
        root = self.state_dir.parent
        try:
            new = node_src.next_mode(node_src.active_sources(root))
            new, off = node_src.set_sources(root, new)
        except OSError as e:
            return self.say(f"не записать переключатель источников: {e}", "bold red")
        self.say(f"источник узлов: {node_src.label(new)} · на паузе из-за источника: {len(off)}", "bold green")
        self._refresh()

    def _pause(self, data: Data) -> None:
        if not self.selected:
            return self.say("выберите аккаунт стрелками ↑/↓", "yellow", 3)
        st = next((a.get("state") for a in (data.snap or {}).get("accounts") or [] if a["name"] == self.selected), None)
        ok, text = toggle_pause(self.state_dir, self.selected, st)
        self.say(text, "bold green" if ok else "bold red")
        if ok:
            self._refresh()

    def _ask_login(self, data: Data, ref: float) -> None:
        if not data.snap:
            return self.say("нет данных о парке — очередь неизвестна", "yellow")
        names = login_targets(data.snap, ref, data.hub, self.selected)
        if not names:
            live = hub_sessions(data.hub, ref)
            return self.say("сессии входа уже открыты (см. «Сессии входа»)" if live else "очередь «перелогинить» пуста",
                            "yellow")
        if hub_sessions(data.hub, ref):
            return self.say("login_hub уже работает — дождитесь окончания его сессий", "yellow")
        self.confirm = names

    def _start_login(self, names: list[str]) -> None:
        if self.launcher is None:
            return self.say("запуск входа недоступен в этом режиме", "yellow")
        self.say(f"запускаю login_hub: {', '.join(names)}…", "bold")

        def work() -> None:
            ok, text = self.launcher.start(names)
            self.hub_password = self.launcher.password if ok else self.hub_password
            self.say(text, "bold green" if ok else "bold red", 10)
            self._refresh()
        self._spawn(work)

    # --- отрисовка ---
    def footer(self, now: float) -> Text:
        if self.confirm is not None:
            t = Text(f" Открыть сессии входа: {', '.join(self.confirm)}?  ", style="bold black on yellow")
            t.append(" y/Enter — да · любая клавиша — нет ", style="bold")
            return t
        if self.msg and self.msg[2] > now:
            return Text(" " + self.msg[0], style=self.msg[1])
        src = node_src.label(node_src.active_sources(self.state_dir.parent))
        return Text(f" q выход · r обновить · s сорт({SORT_LABEL[self.sort]}) · f фильтр({FILTER_LABEL.get(self.flt, self.flt)})"
                    f" · ↑/↓ выбор · l вход · d деталь · p пауза · n узлы({src})", style="dim")

    def render(self, size: tuple[int, int]) -> Layout:
        width, height = size
        data, ref, link, age = self._ctx()
        now = self.clock()
        snap = data.snap
        all_accs = (snap or {}).get("accounts") or []
        vis = self.visible(data, ref)
        queue, first = relogin_queue(snap, ref)
        sessions = hub_sessions(data.hub, now)

        root = Layout()
        root.split_column(Layout(name="head", size=3), Layout(name="mid", size=7),
                          Layout(name="body"), Layout(name="foot", size=1))
        root["head"].update(render_header(data, now, (snap or {}).get("version") or self.version, link, age))
        root["mid"].split_row(Layout(name="nums", size=40), Layout(name="charts"))
        root["nums"].update(render_numbers(snap))
        root["charts"].update(render_charts(snap, max(20, width - 40)))
        side_w = 44 if width >= 110 else max(30, width // 3)
        root["body"].split_row(Layout(name="left"), Layout(name="side", size=side_w))
        body_h = max(4, height - 3 - 7 - 1)
        log_h = min(EVENT_LINES + 2, max(4, body_h // 2))
        root["side"].split_column(Layout(name="relogin"), Layout(name="log", size=log_h))
        root["relogin"].update(render_relogin(queue, first, side_w, max(1, body_h - log_h - 2), ref))
        root["log"].update(render_log(data.events, log_h - 2))

        selected_acc = next((a for a in all_accs if a["name"] == self.selected), None)
        if self.detail and selected_acc:
            root["left"].update(render_detail(selected_acc, data, ref, self.detail_events))
        else:
            hub_h = (len(sessions) + 3) if sessions else 0
            table_h = body_h - hub_h
            rows = max(1, table_h - 5)
            tbl = render_table(vis, data, ref, self.selected, rows, self.sort, self.flt, len(all_accs),
                               width - side_w)
            if sessions:
                root["left"].split_column(Layout(name="tbl"), Layout(name="hub", size=hub_h))
                root["tbl"].update(tbl)
                root["hub"].update(render_hub(sessions, self.hub_password))
            else:
                root["left"].update(tbl)
        root["foot"].update(self.footer(now))
        return root


# ---------------------------------------------------------------- #
#  Режимы --once / --json / живой экран
# ---------------------------------------------------------------- #
def render_once(data: Data, now: float, version: str, width: int = 120, top: int = 20, color: bool = False) -> str:
    """Текстовый снимок (без Layout, без ANSI, если color=False): для логов и чата."""
    link, age = link_state(data, now)
    ref = ref_time(data, now, link)
    snap = data.snap
    queue, first = relogin_queue(snap, ref)
    accs = sort_accounts((snap or {}).get("accounts") or [], "problem", ref)
    buf = StringIO()
    con = Console(file=buf, width=width, force_terminal=color, color_system="standard" if color else None,
                  legacy_windows=False)
    con.print(render_header(data, now, (snap or {}).get("version") or version, link, age))
    con.print(render_numbers(snap, 44))
    con.print(render_charts(snap, width))
    problems = [a for a in accs if is_problem(a)]
    shown = (problems or accs)[:top]
    con.print(render_table(shown, data, ref, None, max(top, 1), "problem", "problems" if problems else "all",
                           len(accs), width))
    if len(problems) > top:
        con.print(Text(f"  … и ещё {len(problems) - top} проблемных (--top N)", style="dim"))
    con.print(render_relogin(queue, first, width, 10 ** 6, ref))
    live = hub_sessions(data.hub, now)
    if live:
        lines = [Text(f"{s.get('account')} порт {s.get('port')} {s.get('status')}  {s.get('link')}") for s in live]
        con.print(Panel(Group(*lines), title="Сессии входа (login_hub)", title_align="left", box=box.ROUNDED))
    con.print(render_log(data.events))
    return buf.getvalue()


def parse_keys(raw: str) -> list[str]:
    """Разбор байтов терминала в имена клавиш: up/down/esc/enter/ctrl-c или символ."""
    keys, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\x1b":
            if raw[i + 1:i + 2] in ("[", "O") and i + 2 < len(raw):
                keys.append({"A": "up", "B": "down"}.get(raw[i + 2], "other"))
                i += 3
                continue
            keys.append("esc")
        elif ch in "\r\n":
            keys.append("enter")
        elif ch == "\x03":
            keys.append("ctrl-c")
        else:
            keys.append(ch)
        i += 1
    return keys


class RawKeys:
    """cbreak-режим stdin (tmux/ssh); терминал восстанавливается при выходе."""

    def __enter__(self) -> "RawKeys":
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self._termios, self._old = termios, termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self._old)

    def read(self, timeout: float) -> list[str]:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return parse_keys(os.read(self.fd, 64).decode("utf-8", errors="ignore")) if ready else []


def run_live(app: App, fetcher: Fetcher | None, console: Console) -> int:
    if not sys.stdin.isatty():
        print("живая панель требует терминал (tty); для логов — --once или --json", file=sys.stderr)
        return 2
    last_sig = None
    with RawKeys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            data_ver = fetcher.get()[1] if fetcher else 0
            sig = (data_ver, int(app.clock()), console.size)
            if sig != last_sig:
                live.update(app.render(console.size), refresh=True)
                last_sig = sig
            pressed = keys.read(0.25)
            for k in pressed:
                if not app.on_key(k):
                    return 0
            if pressed:
                live.update(app.render(console.size), refresh=True)


# ---------------------------------------------------------------- #
#  Заглушка данных (--demo и тесты)
# ---------------------------------------------------------------- #
def demo_data(now: float = 1_700_000_000.0) -> Data:
    import random
    rng = random.Random(7)
    states = ([ACTIVE] * 38 + [COOLDOWN] * 3 + [DEGRADED] * 3 + [AUTH_FAILED] * 4 + [BLOCKED_NODE] * 2
              + [DISABLED] * 1 + [WAITING_LOGIN] * 4)
    accounts = []
    for i, st in enumerate(states, 1):
        a = {"name": f"account_{i}", "state": st, "since": now - rng.randint(60, 86400), "node": f"node-{rng.randint(1, 60):02d}",
             "country": rng.choice(["NL", "DE", "FI", "SE", "PL"]), "exit_ip": f"203.0.113.{i}",
             "requests_1h": rng.randint(150, 420) if st in (ACTIVE, DEGRADED, COOLDOWN) else rng.randint(0, 30),
             "ok_ratio_1h": round(rng.uniform(0.93, 1.0), 3) if st == ACTIVE else (round(rng.uniform(0.4, 0.8), 3) if st != WAITING_LOGIN else None),
             "last_error": None, "last_error_at": None, "last_ok_at": now - rng.randint(1, 20),
             "unique_comments_1h": rng.randint(500, 3000) if st == ACTIVE else 0,
             "cookie_age_hours": round(rng.uniform(1, 40), 1), "needs_login": False, "needs_login_reason": None,
             "series": {k: [rng.randint(0, 9) for _ in range(30)] for k in ("requests", "ok", "unique")}}
        if st == AUTH_FAILED:
            a.update(last_error="auth_403", last_error_at=now - rng.randint(30, 900), needs_login=True,
                     needs_login_reason="auth_403", last_ok_at=now - rng.randint(600, 40000))
        elif st == BLOCKED_NODE:
            a.update(last_error="network", last_error_at=now - 120)
        elif st == DEGRADED:
            a.update(last_error="empty_listing", last_error_at=now - 45)
        elif st == COOLDOWN:
            a.update(last_error="ratelimit_low", last_error_at=now - 20)
        elif st == WAITING_LOGIN:
            a.update(needs_login=True, needs_login_reason="first_login" if i % 2 else "no_cookies",
                     cookie_age_hours=None, last_ok_at=None)
        accounts.append(a)

    def ser(base: float, jit: float) -> list:
        return [round(max(0.0, base + rng.uniform(-jit, jit) + (base * 0.4 if 60 < i < 90 else 0)), 2) for i in range(180)]

    counts = {s: sum(1 for a in accounts if a["state"] == s) for s in set(states)}
    snap = {
        "schema": SCHEMA_VERSION, "ts": now, "started_at": now - 98765, "uptime_s": 98765.0,
        "global": {
            "windows": {w: {"span_s": span, "unique_comments_per_sec": u, "comments_fetched_per_sec": u * 1.9,
                            "dedupe_ratio": 0.47, "sent_to_collector_per_sec": u * 0.98, "send_errors": 2,
                            "requests_per_sec": 14.2, "codes": {"200": 840, "403": 6, "429": 9, "5xx": 2, "other": 1},
                            "empty_listings": 5, "network_errors": 3}
                        for w, span, u in (("10s", 10, 12.4), ("1m", 60, 11.8), ("15m", 900, 12.0), ("total", 98765, 11.5))},
            "queue": {"size": 3700, "max": 10000, "inflight": 4, "degraded": False, "action": "ok", "buffer": 120},
            "accounts": {"total": len(accounts), "active": counts.get(ACTIVE, 0), "cooldown": counts.get(COOLDOWN, 0),
                         "degraded": counts.get(DEGRADED, 0), "auth_failed": counts.get(AUTH_FAILED, 0),
                         "blocked_node": counts.get(BLOCKED_NODE, 0), "disabled": counts.get(DISABLED, 0),
                         "waiting_login": counts.get(WAITING_LOGIN, 0),
                         "needs_login": sum(1 for a in accounts if a["needs_login"])},
            "warning": None, "metrics_errors": 0,
        },
        "series": {"step_s": 5, "points": 180, "ts_end": now, "unique_comments": ser(12, 3), "sent_to_collector": ser(11.8, 3),
                   "requests": ser(14, 3), "ok200_ratio": [min(1.0, round(0.97 - (0.2 if 120 < i < 135 else 0) + rng.uniform(-0.02, 0.02), 3)) for i in range(180)],
                   "empty_listings": [rng.choice([0, 0, 0, 1, 2]) for _ in range(180)],
                   "errors": [rng.choice([0, 0, 1, 2, 5 if 120 < i < 135 else 0]) for i in range(180)]},
        "accounts": accounts,
    }
    events = [
        {"ts": now - 140 + i * 9, "account": f"account_{n}", "event": ev, "detail": d, "level": lvl}
        for i, (n, ev, d, lvl) in enumerate([
            (3, "state_change", "active -> cooldown (ratelimit_low)", "warn"),
            (41, "state_change", "active -> auth_failed (auth_403)", "error"),
            (41, "auth_failed", "auth_403", "error"),
            (3, "state_change", "cooldown -> active", "info"),
            (47, "node_blocked", "node-12", "warn"),
            (47, "node_replaced", "node-12 -> node-31", "info"),
            (22, "relogin_ok", "", "info"),
            (22, "state_change", "auth_failed -> active", "info"),
            (9, "state_change", "active -> degraded (empty_listing)", "warn"),
            (9, "state_change", "degraded -> active", "info"),
            (44, "state_change", "active -> auth_failed (auth_401)", "error"),
            (44, "auth_failed", "auth_401", "error"),
            (13, "state_change", "active -> cooldown (rate_or_server)", "warn"),
            (13, "state_change", "cooldown -> active", "info"),
            (5, "state_change", "active -> degraded (empty_listing)", "warn"),
        ])]
    return Data(snap=snap, source="demo", events=events, hub=None, paused=set())


# ---------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="один текстовый снимок и выход (без ANSI)")
    ap.add_argument("--json", action="store_true", help="машинный вывод (JSON) и выход")
    ap.add_argument("--demo", action="store_true", help="заглушка данных вместо скрапера")
    ap.add_argument("--url", help="адрес /metrics.json (по умолчанию из config.yaml / METRICS_BIND, METRICS_PORT)")
    ap.add_argument("--state-dir", type=Path, default=STATE_DIR, help="каталог state/ (по умолчанию STATE_DIR)")
    ap.add_argument("--interval", type=float, default=1.5, help="период обновления, с (1–2)")
    ap.add_argument("--width", type=int, default=120, help="ширина --once")
    ap.add_argument("--top", type=int, default=20, help="сколько аккаунтов в --once")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)
    interval = min(2.0, max(1.0, args.interval))
    url = args.url or metrics_url()

    if args.once or args.json:
        data = demo_data(time.time()) if args.demo else fetch_all(url, args.state_dir)
        now = time.time()
        if args.json:
            print(json.dumps(summary(data, now), ensure_ascii=False, indent=2))
        else:
            sys.stdout.write(render_once(data, now, git_version(), args.width, args.top))
        return 0

    console = Console(no_color=args.no_color or None)
    if args.demo:
        fetcher = None
        app = App(lambda: demo_data(time.time()), args.state_dir, launcher=None, version="demo")
    else:
        fetcher = Fetcher(url, args.state_dir, interval).start()
        app = App(lambda: fetcher.get()[0], args.state_dir, launcher=HubLauncher(args.state_dir),
                  version=git_version(), refresh=fetcher.refresh)
    try:
        return run_live(app, fetcher, console)
    finally:
        if fetcher:
            fetcher.stop()


if __name__ == "__main__":
    sys.exit(main())
