"""
Модуль 06 — метрики скрапера (контракт данных для панели, модуль 07).

Скрапер ПИШЕТ метрики, панель только читает. Панель не обращается к Reddit и не влияет на скрапер.

Транспорт (схема `schema: 1`, см. docs/metrics-schema.md):
  * HTTP  GET /metrics.json на METRICS_BIND:9100 (по умолчанию 127.0.0.1);
  * резервный файл state/metrics_snapshot.json (атомарная запись раз в snapshot_interval_seconds);
  * журнал событий state/events.jsonl (ротация по размеру).

Устройство:
  * Горячий путь (record_poll / record_send / event) — O(1): инкремент в кольцевых буферах и словарях,
    без ввода-вывода и без сборки JSON. Цикл опроса запись метрик не блокирует.
  * Раз в snapshot_interval_seconds `run()` собирает снимок, сериализует его ОДИН раз и отдаёт те же байты
    и HTTP-клиентам, и в файл; запись файла и журнала идёт в потоке (asyncio.to_thread).
  * Любая ошибка метрик логируется и глотается — метрики не могут уронить скрапер.

Чистая логика (StepSeries, MetricsCollector.snapshot, validate_snapshot) не знает про asyncio и сеть;
время передаётся через clock/wall — поэтому агрегаторы тестируются на синтетических событиях.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import deque
from pathlib import Path

from .constants import log
from .fleet_polling import ACTIVE, ALL_STATES, AUTH_FAILED, BLOCKED_NODE

SCHEMA_VERSION = 1

# --- кольцевые буферы глобальных рядов: 15 минут с шагом 5 с ---
SERIES_STEP = 5.0
SERIES_POINTS = 180

# --- по аккаунту: окно 1 час с шагом 30 с; в снимок уходят последние 15 минут (30 точек) — «сжато» ---
ACC_STEP = 30.0
ACC_POINTS = 120
ACC_SERIES_POINTS = 30

WINDOWS: dict[str, float] = {"10s": 10.0, "1m": 60.0, "15m": 900.0}
HOUR = 3600.0

# Состояния аккаунта, которых нет в планировщике (воркер ещё не зарегистрирован)
WAITING_LOGIN = "waiting_login"   # нет входа / нет cookies — нужен ручной вход
STARTING = "starting"             # включён, cookies есть, воркер ещё не взял слот

CODE_KEYS = ("200", "403", "429", "5xx", "other")
GLOBAL_SERIES = ("unique", "fetched", "sent", "send_errors", "requests",
                 "c200", "c403", "c429", "c5xx", "cother", "empty", "network")

DEFAULT_PORT = 9100
DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------- #
#  Кольцевой буфер со ступенчатыми счётчиками
# ---------------------------------------------------------------- #

class StepSeries:
    """Счётчик по временным корзинам шириной `step` секунд, хранится `size` последних.

    Корзина с номером b = int(now // step) живёт в ячейке b % size; устаревшая ячейка (другой номер)
    считается пустой, поэтому ничего чистить по таймеру не нужно, а add() — O(1)."""

    __slots__ = ("step", "size", "_ids", "_vals")

    def __init__(self, step: float, size: int):
        self.step = float(step)
        self.size = int(size)
        self._ids = [-1] * self.size
        self._vals = [0.0] * self.size

    def add(self, now: float, n: float = 1.0) -> None:
        b = int(now // self.step)
        i = b % self.size
        if self._ids[i] != b:
            self._ids[i] = b
            self._vals[i] = 0.0
        self._vals[i] += n

    def window_sum(self, now: float, seconds: float) -> float:
        """Сумма за последние `seconds` секунд (целые корзины, включая текущую неполную)."""
        cur = int(now // self.step)
        k = min(self.size, max(1, math.ceil(seconds / self.step)))
        total = 0.0
        for b in range(cur - k + 1, cur + 1):
            i = b % self.size
            if self._ids[i] == b:
                total += self._vals[i]
        return total

    def window_span(self, now: float, seconds: float, started: float | None = None) -> float:
        """Сколько секунд реально покрывает window_sum(now, seconds): k-1 целых корзин + часть текущей;
        сразу после старта процесса — не больше аптайма (иначе скорость занижается)."""
        k = min(self.size, max(1, math.ceil(seconds / self.step)))
        span = (k - 1) * self.step + (now - (now // self.step) * self.step)
        if started is not None:
            span = min(span, now - started)
        return max(span, 1.0)

    def values(self, now: float, count: int | None = None) -> list[float]:
        """Последние `count` корзин от старой к новой (пропущенные — нули); последняя — текущая неполная."""
        count = self.size if count is None else min(count, self.size)
        cur = int(now // self.step)
        out = []
        for b in range(cur - count + 1, cur + 1):
            i = b % self.size
            out.append(self._vals[i] if self._ids[i] == b else 0.0)
        return out


def _num(x: float, nd: int = 3):
    return round(float(x), nd)


def _count(x: float) -> int:
    return int(round(x))


# ---------------------------------------------------------------- #
#  Журнал событий (state/events.jsonl, ротация по размеру)
# ---------------------------------------------------------------- #

class EventJournal:
    """emit() только кладёт строку в память; flush() (в потоке) дописывает в файл и ротирует.
    Формат строки: {"ts", "account", "event", "detail"} (+ "level" для цвета в панели)."""

    def __init__(self, path: Path, max_bytes: int = 5 * 1024 * 1024, backups: int = 3, keep_memory: int = 200):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self._pending: deque[str] = deque(maxlen=10000)
        self.recent: deque[dict] = deque(maxlen=keep_memory)

    def emit(self, record: dict) -> None:
        self.recent.append(record)
        self._pending.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))

    def flush(self) -> int:
        """Дописать накопленное; возвращает число записанных строк. Ошибки ввода-вывода — наружу (ловит run())."""
        if not self._pending:
            return 0
        lines = []
        while self._pending:
            lines.append(self._pending.popleft())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed(sum(len(s) + 1 for s in lines))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        try:
            os.chmod(self.path, 0o666)
        except OSError:
            pass
        return len(lines)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        # events.jsonl -> .1 -> .2 ...; самый старый удаляется
        for n in range(self.backups, 0, -1):
            src = self.path if n == 1 else self.path.with_name(f"{self.path.name}.{n - 1}")
            dst = self.path.with_name(f"{self.path.name}.{n}")
            if src.exists():
                os.replace(src, dst)
        if self.backups <= 0:
            try:
                self.path.unlink()
            except OSError:
                pass


def read_events(path: Path, limit: int = 15) -> list[dict]:
    """Последние `limit` событий (для панели). Нет файла / мусор в строках — не ошибка."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in tail[-limit * 3:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-limit:]


# ---------------------------------------------------------------- #
#  Метрики одного аккаунта
# ---------------------------------------------------------------- #

class _Acc:
    __slots__ = ("name", "state", "since", "requests", "ok", "unique", "last_error", "last_error_at",
                 "last_ok_at", "last_status", "seen")

    def __init__(self, name: str):
        self.name = name
        self.state: str | None = None
        self.since: float | None = None       # monotonic начала текущего состояния
        self.requests = StepSeries(ACC_STEP, ACC_POINTS)
        self.ok = StepSeries(ACC_STEP, ACC_POINTS)
        self.unique = StepSeries(ACC_STEP, ACC_POINTS)
        self.last_error: str | None = None
        self.last_error_at: float | None = None   # monotonic
        self.last_ok_at: float | None = None      # monotonic
        self.last_status: int | None = None
        self.seen = False                         # хотя бы раз слышали о воркере (опрос/состояние)


def _code_bucket(status: int | None) -> str | None:
    if status is None:
        return None
    if status in (200, 403, 429):
        return str(status)
    if status >= 500:
        return "5xx"
    return "other"


def error_label(kind: str | None, status: int | None) -> str | None:
    """Метка последней ошибки — те же значения, что у AccountHealth.last_error."""
    if kind == "auth":
        return f"auth_{status}" if status else "auth"
    if kind == "empty":
        return "empty_listing"
    if kind in ("rate_or_server", "network"):
        return kind
    return None


class MetricsCollector:
    """Сборщик метрик. Все публичные record_*/event — синхронные и дешёвые (горячий путь)."""

    def __init__(self, clock=time.monotonic, wall=time.time, state_dir: Path | str | None = None,
                 journal: EventJournal | None = None):
        self._clock = clock
        self._wall = wall
        self.started = clock()
        self.started_wall = wall()
        self.state_dir = Path(state_dir) if state_dir else None
        self.journal = journal

        self._g = {k: StepSeries(SERIES_STEP, SERIES_POINTS) for k in GLOBAL_SERIES}
        self._totals = {k: 0 for k in GLOBAL_SERIES}
        self._acc: dict[str, _Acc] = {}
        self._cfg_accounts: list[dict] = []
        self._scheduler = None
        self._sender = None
        self._ident: dict[str, dict] = {}     # name -> {node, country, exit_ip, cookie_mtime}
        self.errors = 0                       # сколько раз сбор метрик падал (для диагностики)

    # ---- подключение источников ----
    def attach(self, scheduler=None, sender=None, accounts: list[dict] | None = None) -> None:
        self._scheduler = scheduler
        self._sender = sender
        if accounts is not None:
            self._cfg_accounts = [a for a in accounts if a.get("name")]
            for a in self._cfg_accounts:
                self._acc_of(a["name"])

    def _acc_of(self, name: str) -> _Acc:
        a = self._acc.get(name)
        if a is None:
            a = self._acc[name] = _Acc(name)
        return a

    # ---- горячий путь ----
    def record_poll(self, account: str, kind: str, status: int | None = None, requests: int = 1,
                    fetched: int = 0, unique: int = 0, state: str | None = None) -> None:
        """Итог одного цикла опроса аккаунта.

        kind: ok | empty | rate_or_server | auth | network (skipped сюда не передаётся);
        requests — сколько HTTP-запросов сделано (страниц при пагинации); fetched — комментариев получено
        (с дублями); unique — ушло в буфер отправки; state — состояние аккаунта ПОСЛЕ опроса."""
        try:
            now = self._clock()
            requests = max(1, int(requests))
            a = self._acc_of(account)
            a.seen = True
            a.requests.add(now, requests)
            self._bump("requests", now, requests)

            code = _code_bucket(200 if kind in ("ok", "empty") else status)
            if code:
                self._bump({"200": "c200", "403": "c403", "429": "c429", "5xx": "c5xx", "other": "cother"}[code],
                           now, requests if kind == "ok" else 1)
            if kind == "ok":
                a.ok.add(now, requests)
                a.last_ok_at = now
                if fetched:
                    self._bump("fetched", now, fetched)
                if unique:
                    self._bump("unique", now, unique)
                    a.unique.add(now, unique)
            elif kind == "empty":
                self._bump("empty", now)
            elif kind == "network":
                self._bump("network", now)

            label = error_label(kind, status)
            if label:
                a.last_error, a.last_error_at, a.last_status = label, now, status
            if state is not None:
                self._note_state(a, state, now, reason=label)
        except Exception:  # метрики никогда не роняют опрос
            self._failed("record_poll")

    def record_send(self, sent: int, failed: int = 0) -> None:
        """Итог отправки батча в коллектор: sent подтверждено, failed — нет (в т.ч. ошибка сети/5xx)."""
        try:
            now = self._clock()
            if sent:
                self._bump("sent", now, sent)
            if failed:
                self._bump("send_errors", now, failed)
        except Exception:
            self._failed("record_send")

    def event(self, account: str | None, event: str, detail: str = "", level: str = "info") -> None:
        """Событие в журнал (state/events.jsonl). Только в память — запись в файл делает flush в потоке."""
        try:
            rec = {"ts": round(self._wall(), 3), "account": account, "event": event,
                   "detail": detail, "level": level}
            if self.journal is not None:
                self.journal.emit(rec)
        except Exception:
            self._failed("event")

    def observe_states(self, states: dict[str, tuple[str, float | None]]) -> None:
        """Сверка состояний со снимком планировщика: {name: (effective_state, since_monotonic)}.
        Ловит переходы без опроса (cooldown истёк, disabled включён/выключен оператором)."""
        now = self._clock()
        for name, (state, since) in states.items():
            a = self._acc_of(name)
            a.seen = True
            self._note_state(a, state, now, since=since)

    # ---- внутреннее ----
    def _bump(self, key: str, now: float, n: float = 1) -> None:
        self._g[key].add(now, n)
        self._totals[key] += n

    def _failed(self, where: str) -> None:
        self.errors += 1
        if self.errors <= 3 or self.errors % 1000 == 0:
            log.exception("[metrics] ошибка в %s (метрики отключать не нужно, скрапер не затронут), всего: %d",
                          where, self.errors)

    def _note_state(self, a: _Acc, state: str, now: float, since: float | None = None, reason: str | None = None):
        prev = a.state
        if prev == state:
            return
        a.state = state
        a.since = since if since is not None else now
        if prev is None:
            return  # первое наблюдение — это не переход
        reason = reason or a.last_error
        detail = f"{prev} -> {state}" + (f" ({reason})" if reason else "")
        level = "error" if state in (AUTH_FAILED, BLOCKED_NODE) else ("warn" if state != ACTIVE else "info")
        self.event(a.name, "state_change", detail, level)
        if state == AUTH_FAILED:
            self.event(a.name, "auth_failed", reason or "", "error")
        elif state == BLOCKED_NODE:
            self.event(a.name, "node_blocked", reason or "", "error")

    # ---- идентичность аккаунта (нода / страна / IP / возраст cookies) ----
    def refresh_identity(self) -> None:
        """Перечитать state/slots, state/profiles и mtime cookies. Блокирующий ввод-вывод — вызывать в потоке."""
        if self.state_dir is None:
            return
        out: dict[str, dict] = {}
        for acc in self._cfg_accounts:
            name = acc["name"]
            row: dict = {"node": None, "country": None, "exit_ip": None, "cookie_mtime": None}
            slot = _read_json(self.state_dir / "slots" / f"{name}.json")
            if isinstance(slot, dict):
                row["node"] = slot.get("node")
                row["country"] = slot.get("country")
            prof = _read_json(self.state_dir / "profiles" / f"{name}.json")
            if isinstance(prof, dict):
                row["country"] = prof.get("country") or row["country"]
                row["exit_ip"] = prof.get("exit_ip")
            cf = acc.get("cookie_file")
            if cf:
                p = Path(cf)
                if not p.is_absolute():
                    p = self._base_dir() / p
                try:
                    row["cookie_mtime"] = p.stat().st_mtime
                except OSError:
                    row["cookie_mtime"] = None
            out[name] = row
        self._ident = out

    @staticmethod
    def _base_dir() -> Path:
        from .constants import BASE_DIR
        return BASE_DIR

    # ---- снимок ----
    def _window(self, now: float, seconds: float | None) -> dict:
        """Окно в секундах; None — «суммарно» (со старта процесса)."""
        g = self._g
        if seconds is None:
            span = max(now - self.started, 1.0)

            def s(k):
                return self._totals[k]
        else:
            span = g["requests"].window_span(now, seconds, self.started)

            def s(k):
                return g[k].window_sum(now, seconds)

        fetched, unique = s("fetched"), s("unique")
        return {
            "span_s": _num(span, 1),
            "unique_comments_per_sec": _num(unique / span),
            "comments_fetched_per_sec": _num(fetched / span),
            "dedupe_ratio": _num(1.0 - unique / fetched) if fetched > 0 else None,
            "sent_to_collector_per_sec": _num(s("sent") / span),
            "send_errors": _count(s("send_errors")),
            "requests_per_sec": _num(s("requests") / span),
            "codes": {"200": _count(s("c200")), "403": _count(s("c403")), "429": _count(s("c429")),
                      "5xx": _count(s("c5xx")), "other": _count(s("cother"))},
            "empty_listings": _count(s("empty")),
            "network_errors": _count(s("network")),
        }

    def _queue(self) -> dict:
        q = {"size": None, "max": None, "inflight": None, "degraded": None, "action": None, "buffer": None}
        if self._sender is not None:
            try:
                q.update(self._sender.queue_info())
            except Exception:
                self._failed("queue_info")
        return q

    def snapshot(self) -> dict:
        now = self._clock()
        wall = self._wall()
        sched = self._scheduler
        slot_names: set[str] = set()
        warning = None
        if sched is not None:
            states = {}
            for s in list(sched.slots):
                states[s.name] = (s.health.effective_state(now), s.health.since)
                slot_names.add(s.name)
                a = self._acc_of(s.name)
                # источник правды по последней ошибке/успеху — состояние планировщика (учитывает emergency-ratelimit)
                h = s.health
                if h.last_error and (a.last_error_at is None or (h.last_error_at or 0) >= a.last_error_at):
                    a.last_error, a.last_error_at = h.last_error, h.last_error_at
                if h.last_ok_at is not None:
                    a.last_ok_at = h.last_ok_at
                if h.last_auth_status is not None:
                    a.last_status = h.last_auth_status
            self.observe_states(states)
            try:
                warning = sched.snapshot().get("warning")
            except Exception:
                self._failed("scheduler.snapshot")

        cfg_by_name = {a["name"]: a for a in self._cfg_accounts}
        names = list(cfg_by_name) + [n for n in self._acc if n not in cfg_by_name and self._acc[n].seen]
        accounts = []
        counts = {st: 0 for st in ALL_STATES}
        counts[WAITING_LOGIN] = 0
        counts[STARTING] = 0
        for name in names:
            row = self._account_row(name, cfg_by_name.get(name), now, wall,
                                    running=(name in slot_names) if sched is not None else None)
            accounts.append(row)
            st = row["state"]
            counts[st] = counts.get(st, 0) + 1

        gl = {k: self._g[k].values(now, SERIES_POINTS) for k in GLOBAL_SERIES}
        req = gl["requests"]
        ok200 = [(c / r) if r else None for c, r in zip(gl["c200"], req)]
        errs = [max(0.0, r - c) for r, c in zip(req, gl["c200"])]
        series = {
            "unique_comments": [_num(v / SERIES_STEP) for v in gl["unique"]],
            "sent_to_collector": [_num(v / SERIES_STEP) for v in gl["sent"]],
            "requests": [_num(v / SERIES_STEP) for v in req],
            "ok200_ratio": [None if v is None else _num(v) for v in ok200],
            "empty_listings": [_count(v) for v in gl["empty"]],
            "errors": [_count(v) for v in errs],
        }

        return {
            "schema": SCHEMA_VERSION,
            "ts": _num(wall, 3),
            "started_at": _num(self.started_wall, 3),
            "uptime_s": _num(now - self.started, 1),
            "global": {
                "windows": {**{k: self._window(now, w) for k, w in WINDOWS.items()},
                            "total": self._window(now, None)},
                "queue": self._queue(),
                "accounts": {
                    "total": len(accounts),
                    "active": counts[ACTIVE],
                    "cooldown": counts["cooldown"],
                    "degraded": counts["degraded"],
                    "auth_failed": counts[AUTH_FAILED],
                    "blocked_node": counts[BLOCKED_NODE],
                    "disabled": counts["disabled"],
                    "waiting_login": counts[WAITING_LOGIN],
                    "needs_login": sum(1 for r in accounts if r["needs_login"]),
                },
                "warning": warning,
                "metrics_errors": self.errors,
            },
            "series": {"step_s": SERIES_STEP, "points": SERIES_POINTS, "ts_end": _num(wall, 3), **series},
            "accounts": accounts,
        }

    def _account_row(self, name: str, cfg: dict | None, now: float, wall: float, running: bool | None) -> dict:
        a = self._acc_of(name)
        ident = self._ident.get(name) or {}
        cookie_mtime = ident.get("cookie_mtime")
        has_cookies = (cookie_mtime is not None) if self._ident else None
        enabled = True if cfg is None else bool(cfg.get("enabled"))
        login = bool(cfg and cfg.get("login"))

        def w(t):  # monotonic -> wall
            return None if t is None else _num(wall - (now - t), 3)

        needs_login, reason = False, None
        state = a.state
        if cfg is not None and not enabled:
            state, a.since = WAITING_LOGIN if login else "disabled", a.since or now
            if login:
                needs_login, reason = True, "first_login"
        elif running is False or state is None:
            if has_cookies is False:
                state, needs_login, reason = WAITING_LOGIN, True, "no_cookies"
            else:
                state = STARTING
            a.since = a.since or now
        if state == AUTH_FAILED:
            needs_login, reason = True, a.last_error or "auth_failed"

        req_1h = a.requests.window_sum(now, HOUR)
        ok_1h = a.ok.window_sum(now, HOUR)
        return {
            "name": name,
            "state": state,
            "since": w(a.since),
            "node": ident.get("node"),
            "country": ident.get("country"),
            "exit_ip": ident.get("exit_ip"),
            "requests_1h": _count(req_1h),
            "ok_ratio_1h": _num(ok_1h / req_1h) if req_1h > 0 else None,
            "last_error": a.last_error,
            "last_error_at": w(a.last_error_at),
            "last_ok_at": w(a.last_ok_at),
            "unique_comments_1h": _count(a.unique.window_sum(now, HOUR)),
            "cookie_age_hours": None if cookie_mtime is None else _num(max(0.0, (wall - cookie_mtime) / 3600.0), 2),
            "needs_login": needs_login,
            "needs_login_reason": reason,
            "series": {
                "step_s": ACC_STEP,
                "requests": [_count(v) for v in a.requests.values(now, ACC_SERIES_POINTS)],
                "ok": [_count(v) for v in a.ok.values(now, ACC_SERIES_POINTS)],
                "unique": [_count(v) for v in a.unique.values(now, ACC_SERIES_POINTS)],
            },
        }

    # ---- фоновая задача ----
    async def run(self, snapshot_path: Path | None, interval: float = 2.0, identity_every: float = 30.0,
                  publish=None) -> None:
        """Раз в `interval` секунд: снимок -> байты (публикуются HTTP-клиентам) -> файл и журнал в потоке."""
        last_ident = -1e9
        while True:
            try:
                now = self._clock()
                if now - last_ident >= identity_every:
                    last_ident = now
                    await asyncio.to_thread(self.refresh_identity)
                snap = self.snapshot()
                body = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if publish is not None:
                    publish(body)
                if snapshot_path is not None or self.journal is not None:
                    await asyncio.to_thread(self._write_out, snapshot_path, body)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed("run")
            await asyncio.sleep(interval)

    def _write_out(self, path: Path | None, body: bytes) -> None:
        if path is not None:
            try:
                write_bytes_atomic(Path(path), body)
            except OSError as e:
                log.warning("[metrics] не смог записать %s: %s", path, e)
        if self.journal is not None:
            try:
                self.journal.flush()
            except OSError as e:
                log.warning("[metrics] не смог записать журнал событий %s: %s", self.journal.path, e)


# ---------------------------------------------------------------- #
#  Файл / HTTP
# ---------------------------------------------------------------- #

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_bytes_atomic(path: Path, body: bytes) -> None:
    """tmp в той же директории + os.replace; без fsync (снимок пишется каждые 2 с и перезаписывается)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(body)
    try:
        os.chmod(tmp, 0o666)
    except OSError:
        pass
    os.replace(tmp, path)


class MetricsServer:
    """GET /metrics.json — отдаёт последние готовые байты (ничего не считает на запросе)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self._body: bytes = b""
        self._runner = None
        self.bound_port: int | None = None

    def publish(self, body: bytes) -> None:
        self._body = body

    async def _handle(self, request):
        from aiohttp import web
        if not self._body:
            return web.json_response({"schema": SCHEMA_VERSION, "error": "no_data_yet"}, status=503)
        return web.Response(body=self._body, content_type="application/json", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    async def start(self) -> bool:
        """Поднять сервер. Порт занят и т.п. — не фатально: остаётся файл-снимок."""
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/metrics.json", self._handle)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except OSError as e:
            log.warning("[metrics] HTTP %s:%s не поднялся (%s) — остаётся только state/metrics_snapshot.json",
                        self.host, self.port, e)
            await runner.cleanup()
            return False
        self._runner = runner
        try:
            self.bound_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        except Exception:
            self.bound_port = self.port
        log.info("[metrics] GET http://%s:%s/metrics.json", self.host, self.bound_port)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def serve_metrics(collector: MetricsCollector, cfg: dict, state_dir: Path) -> None:
    """Задача для main.py: HTTP + снимок-файл + журнал. Не входит в «ядро» — падение не останавливает скрапер."""
    host = os.environ.get("METRICS_BIND") or str(cfg.get("host", DEFAULT_HOST))
    port = int(os.environ.get("METRICS_PORT") or cfg.get("port", DEFAULT_PORT))
    server = MetricsServer(host, port)
    try:
        await server.start()
        await collector.run(
            snapshot_path=state_dir / "metrics_snapshot.json",
            interval=float(cfg.get("snapshot_interval_seconds", 2.0)),
            identity_every=float(cfg.get("identity_refresh_seconds", 30.0)),
            publish=server.publish,
        )
    finally:
        await server.stop()


# ---------------------------------------------------------------- #
#  Чтение и проверка снимка (для панели, модуль 07, и для тестов)
# ---------------------------------------------------------------- #

def read_snapshot(path: Path) -> dict | None:
    """Снимок с диска. Нет файла / битый JSON / чужая схема -> None («нет данных»), исключений нет."""
    data = _read_json(Path(path))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return None
    return data


_WINDOW_FIELDS = ("span_s", "unique_comments_per_sec", "comments_fetched_per_sec", "dedupe_ratio",
                  "sent_to_collector_per_sec", "send_errors", "requests_per_sec", "codes",
                  "empty_listings", "network_errors")
_ACCOUNT_FIELDS = ("name", "state", "since", "node", "country", "exit_ip", "requests_1h", "ok_ratio_1h",
                   "last_error", "last_error_at", "last_ok_at", "unique_comments_1h", "cookie_age_hours",
                   "needs_login", "needs_login_reason", "series")
_ACCOUNT_COUNTS = ("total", "active", "cooldown", "degraded", "auth_failed", "blocked_node", "disabled",
                   "waiting_login", "needs_login")
_SERIES_KEYS = ("unique_comments", "sent_to_collector", "requests", "ok200_ratio", "empty_listings", "errors")
_ACCOUNT_STATES = set(ALL_STATES) | {WAITING_LOGIN, STARTING}


def validate_snapshot(snap) -> list[str]:
    """Список нарушений контракта `schema: 1`; пустой список — снимок валиден."""
    errs: list[str] = []
    if not isinstance(snap, dict):
        return ["снимок не объект"]
    if snap.get("schema") != SCHEMA_VERSION:
        errs.append(f"schema != {SCHEMA_VERSION}")
    for k in ("ts", "started_at", "uptime_s"):
        if not isinstance(snap.get(k), (int, float)):
            errs.append(f"{k}: не число")

    g = snap.get("global")
    if not isinstance(g, dict):
        return errs + ["global: нет"]
    wins = g.get("windows")
    if not isinstance(wins, dict):
        errs.append("global.windows: нет")
    else:
        for w in (*WINDOWS, "total"):
            row = wins.get(w)
            if not isinstance(row, dict):
                errs.append(f"global.windows.{w}: нет")
                continue
            for f in _WINDOW_FIELDS:
                if f not in row:
                    errs.append(f"global.windows.{w}.{f}: нет")
            for f in ("unique_comments_per_sec", "comments_fetched_per_sec", "sent_to_collector_per_sec",
                      "requests_per_sec"):
                v = row.get(f)
                if not isinstance(v, (int, float)) or v < 0:
                    errs.append(f"global.windows.{w}.{f}: ожидалось число >= 0")
            dr = row.get("dedupe_ratio")
            if dr is not None and not (isinstance(dr, (int, float)) and -1e-9 <= dr <= 1 + 1e-9):
                errs.append(f"global.windows.{w}.dedupe_ratio: ожидалось null или 0..1")
            codes = row.get("codes")
            if not isinstance(codes, dict) or any(c not in codes for c in CODE_KEYS):
                errs.append(f"global.windows.{w}.codes: нужны ключи {CODE_KEYS}")
    q = g.get("queue")
    if not isinstance(q, dict) or any(k not in q for k in ("size", "max", "degraded", "action")):
        errs.append("global.queue: нужны size, max, degraded, action")
    ac = g.get("accounts")
    if not isinstance(ac, dict) or any(not isinstance(ac.get(k), int) for k in _ACCOUNT_COUNTS):
        errs.append(f"global.accounts: нужны целые {_ACCOUNT_COUNTS}")

    ser = snap.get("series")
    if not isinstance(ser, dict):
        errs.append("series: нет")
    else:
        pts = ser.get("points")
        for k in _SERIES_KEYS:
            v = ser.get(k)
            if not isinstance(v, list) or len(v) != pts:
                errs.append(f"series.{k}: ожидался список из {pts} точек")

    accs = snap.get("accounts")
    if not isinstance(accs, list):
        errs.append("accounts: нет")
    else:
        names = set()
        for i, row in enumerate(accs):
            if not isinstance(row, dict):
                errs.append(f"accounts[{i}]: не объект")
                continue
            for f in _ACCOUNT_FIELDS:
                if f not in row:
                    errs.append(f"accounts[{i}].{f}: нет")
            if row.get("name") in names:
                errs.append(f"accounts[{i}].name: дубликат {row.get('name')}")
            names.add(row.get("name"))
            if row.get("state") not in _ACCOUNT_STATES:
                errs.append(f"accounts[{i}].state: неизвестное {row.get('state')!r}")
            if not isinstance(row.get("needs_login"), bool):
                errs.append(f"accounts[{i}].needs_login: не bool")
            r = row.get("ok_ratio_1h")
            if r is not None and not (isinstance(r, (int, float)) and 0 <= r <= 1 + 1e-9):
                errs.append(f"accounts[{i}].ok_ratio_1h: ожидалось null или 0..1")
        if isinstance(ac, dict) and ac.get("total") != len(accs):
            errs.append("global.accounts.total != len(accounts)")
    return errs

