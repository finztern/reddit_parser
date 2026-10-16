"""
Единый планировщик опроса парка (модуль 05).

Темп диктует ПОТОК, а не аккаунты:

  1. Общий темп парка T_fleet (запросов/с) — один на весь парк. В режиме
     `target_fleet_rate: auto` он подстраивается по доле дублей на первой
     странице ответа (overlap_target_low/high, как раньше): мало дублей —
     опрашиваем чаще, много — реже. Число — фиксированный темп.
  2. Запросы раздаются ПО КРУГУ (round-robin): слот получает тот из готовых
     аккаунтов, что дольше всех не опрашивался. Средний интервал на аккаунт
     получается interval_acc = N_healthy / T_fleet (джиттер ±jitter_ratio).
  3. Нижняя граница паузы аккаунта: после каждого запроса аккаунт «отдыхает»
     не меньше случайных pause_min..pause_max секунд (по умолчанию 3–7).
     Если парк слишком мал для целевого темпа (interval_acc < средней
     паузы), темп физически не набрать — в snapshot() выставляется
     warning="not_enough_accounts" (панель покажет «не хватает аккаунтов»).
  4. Лимиты Reddit (429, Retry-After, аномальные X-Ratelimit-*) — только
     аварийный тормоз: аккаунт уходит в cooldown с экспоненциальной паузой,
     остальные подхватывают его долю (слоты глобальные).
  5. У каждого аккаунта своё состояние (scraper/fleet_polling.py): active,
     cooldown, degraded, auth_failed, blocked_node, disabled. Аккаунты вне
     карусели (auth_failed / blocked_node) получают только редкие пробы и
     не отнимают слоты у общего темпа.

Опрос идёт ПО СПРОСУ: слоты выдаются, только пока Sender.demand() говорит, что
буфер отправки не полон (DEMAND_NONE — ждём, темп парка «снижается» до нуля,
распределение между аккаунтами сохраняется). DEMAND_URGENT поднимает темп до
максимума (stream_min_interval_seconds), если target_fleet_rate: auto.
"""

import asyncio
import json
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .fleet_polling import (
    ACTIVE, ALL_STATES, AUTH_FAILED, BLOCKED_NODE, DEGRADED, DISABLED, OUT_OF_ROTATION,
    AccountHealth, FleetPollingConfig,
)

WARN_NOT_ENOUGH_ACCOUNTS = "not_enough_accounts"
# Минимальный интервал между слотами парка при фиксированном target_fleet_rate
# (защита от busy-loop при опечатке в конфиге).
MIN_FLEET_INTERVAL = 0.05
# Доля времени «ни одного готового аккаунта при спросе», выше которой
# считаем, что парку не хватает аккаунтов (даже если арифметика сходится).
STARVED_WARN_RATIO = 0.2


@dataclass
class PollOutcome:
    page_len: int         # сколько комментариев на первой странице
    dupes: int            # из них уже виденных (до claim)
    pages: int            # сколько страниц выкачано
    overlap_found: bool   # дошли ли до уже виденных


class AccountSlot:
    __slots__ = ("name", "not_before", "busy", "alive", "queue", "last_poll", "after_pause", "health")

    def __init__(self, name: str, now: float = 0.0, rng=random):
        self.name = name
        self.not_before = 0.0
        self.last_poll = 0.0        # monotonic последней выдачи слота (0 = ещё не опрашивался)
        self.after_pause = False    # слот выдан после простоя по спросу — доле дублей верить нельзя
        self.busy = False
        self.alive = True
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.health = AccountHealth(name, now, rng)


class PollScheduler:
    def __init__(self, get, demand=None, clock=time.monotonic, rng=random):
        self._get = get  # ConfigStore.get — конфиг читается на лету
        self._demand = demand  # Sender.demand: 0 — не опрашивать, 1 — обычно, 2 — срочно
        self._clock = clock
        self._rng = rng
        self._idle = False     # был простой из-за полного буфера
        self.slots: list[AccountSlot] = []
        self.interval = float(get("stream_start_interval_seconds", 1.5))  # адаптивный интервал парка
        self._ewma_overlap: float | None = None
        self._sent = deque()    # (ts, n) отправленных уникальных
        self._polls = deque()   # ts опросов
        self._starved = deque() # (ts, длительность) простоя «спрос есть, готовых аккаунтов нет»
        self._warn_logged_at: float | None = None
        self._last_warning: str | None = None
        # накопительные счётчики (для метрик модуля 06)
        self.totals = {"polls": 0, "ok": 0, "empty": 0, "rate_or_server": 0,
                       "auth": 0, "network": 0, "skipped": 0, "unique": 0}

    # ---- конфиг ----
    def cfg(self) -> FleetPollingConfig:
        return FleetPollingConfig.from_getter(self._get)

    def now(self) -> float:
        return self._clock()

    # ---- регистрация ----
    def register(self, name: str) -> AccountSlot:
        slot = AccountSlot(name, self._clock(), self._rng)
        self.slots.append(slot)
        return slot

    def unregister(self, slot: AccountSlot):
        slot.alive = False
        if slot in self.slots:
            self.slots.remove(slot)

    # ---- вызывается воркером после каждого цикла (в т.ч. ошибочного) ----
    def release(self, slot: AccountSlot, outcome: PollOutcome | None,
                cooldown: float, sent: int, kind: str | None = None):
        """kind: ok | empty | rate_or_server | auth | network | skipped (для счётчиков)."""
        now = self._clock()
        cfg = self.cfg()
        slot.busy = False
        h = slot.health

        if h.state == DISABLED:
            pause = cfg.recovery_probe_seconds
        elif h.state in (AUTH_FAILED, BLOCKED_NODE):
            # вне карусели: редкие пробы (новые cookies / замена ноды подхватываются воркером)
            pause = h.probe_pause(cfg) if h.state == AUTH_FAILED else self._blocked_probe(cfg)
            pause = max(pause, cooldown)
        else:
            # нижняя граница паузы аккаунта + собственный кулдаун + аварийный тормоз
            pause = max(cfg.pick_pause(self._rng), cooldown, h.cooldown_until - now)
        slot.not_before = now + max(0.0, pause)

        if kind == "skipped":
            # воркер ждёт демона ротации и Reddit не трогал — это не опрос
            self.totals["skipped"] += 1
            slot.after_pause = False
            return
        self._polls.append(now)
        self.totals["polls"] += 1
        if kind in self.totals:
            self.totals[kind] += 1
        if sent:
            self._sent.append((now, sent))
            self.totals["unique"] += sent
        # после простоя (полный буфер) первая страница не пересекается с
        # виденным не из-за пропуска, а из-за паузы — не подстраиваем интервал
        if outcome is not None and not slot.after_pause:
            self._adapt(outcome)
        slot.after_pause = False

    @staticmethod
    def _blocked_probe(cfg: FleetPollingConfig) -> float:
        # нода заменяется демоном за минуты: заглядываем чаще, чем в auth_failed
        return max(5.0, cfg.recovery_probe_seconds / 4.0)

    def _adapt(self, o: PollOutcome):
        g = self._get
        if o.page_len < g("full_page_threshold", 90):
            return  # неполная страница — по ней о пропусках судить нельзя
        frac = o.dupes / o.page_len
        a = 0.3
        self._ewma_overlap = frac if self._ewma_overlap is None else (
            a * frac + (1 - a) * self._ewma_overlap
        )
        lo = g("overlap_target_low", 0.25)
        hi = g("overlap_target_high", 0.5)
        if o.dupes == 0:
            self.interval *= 0.8          # пропуск — реагируем сразу
        elif self._ewma_overlap < lo:
            self.interval *= 0.93
        elif self._ewma_overlap > hi:
            self.interval *= 1.07
        self.interval = max(
            g("stream_min_interval_seconds", 0.4),
            min(g("stream_max_interval_seconds", 6.0), self.interval),
        )

    # ---- темп парка ----
    def fleet_interval(self, cfg: FleetPollingConfig | None = None, mode: int = 1) -> float:
        """Текущий интервал между слотами парка (до джиттера)."""
        cfg = cfg or self.cfg()
        if cfg.target_fleet_rate:
            return max(MIN_FLEET_INTERVAL, 1.0 / cfg.target_fleet_rate)
        if mode >= 2:  # очереди пайплайна не хватает данных — максимальный темп
            return float(self._get("stream_min_interval_seconds", 0.4))
        return self.interval

    def _sync_disabled(self, cfg: FleetPollingConfig, now: float):
        off = set(cfg.disabled_accounts)
        for s in self.slots:
            want = s.name in off
            if want != (s.health.state == DISABLED):
                s.health.set_disabled(now, want)

    # ---- выбор аккаунта ----
    def _pick(self, now: float) -> AccountSlot | None:
        """Round-robin: среди готовых аккаунтов карусели — тот, что дольше всех не опрашивался."""
        best = None
        for s in self.slots:
            if s.alive and not s.busy and s.not_before <= now and s.health.state not in OUT_OF_ROTATION:
                if best is None or s.last_poll < best.last_poll:
                    best = s
        return best

    def _pick_probe(self, now: float) -> AccountSlot | None:
        """Аккаунт вне карусели, которому пора сделать пробу (новые cookies / новая нода)."""
        best = None
        for s in self.slots:
            if (s.alive and not s.busy and s.not_before <= now
                    and s.health.state in (AUTH_FAILED, BLOCKED_NODE)):
                if best is None or s.last_poll < best.last_poll:
                    best = s
        return best

    def _issue(self, slot: AccountSlot, now: float, after_pause: bool):
        slot.busy = True
        slot.last_poll = now
        slot.after_pause = after_pause
        slot.queue.put_nowait(True)

    async def run(self):
        while True:
            mode = self._demand() if self._demand else 1
            if mode <= 0:
                self._idle = True
                await asyncio.sleep(0.05)
                continue
            now = self._clock()
            cfg = self.cfg()
            self._sync_disabled(cfg, now)

            # пробы аккаунтов вне карусели не занимают слот темпа парка
            probe = self._pick_probe(now)
            if probe is not None:
                self._issue(probe, now, after_pause=True)

            slot = self._pick(now)
            if slot is None:
                self._starved.append((now, 0.05))
                await asyncio.sleep(0.05)
                continue
            self._issue(slot, now, after_pause=self._idle)
            self._idle = False

            interval = self.fleet_interval(cfg, mode)
            j = cfg.jitter_ratio
            await asyncio.sleep(interval * self._rng.uniform(1 - j, 1 + j))

    # ---- состояние парка ----
    def state_counts(self, now: float | None = None) -> dict[str, int]:
        now = self._clock() if now is None else now
        counts = {s: 0 for s in ALL_STATES}
        for s in self.slots:
            counts[s.health.effective_state(now)] += 1
        return counts

    def _window_trim(self, now: float, window: float):
        while self._sent and now - self._sent[0][0] > window:
            self._sent.popleft()
        while self._polls and now - self._polls[0] > window:
            self._polls.popleft()
        while self._starved and now - self._starved[0][0] > window:
            self._starved.popleft()

    def snapshot(self, window: float = 30.0) -> dict:
        """Срез состояния парка: для панели (модули 06/07) и для лога."""
        now = self._clock()
        cfg = self.cfg()
        self._window_trim(now, window)
        counts = self.state_counts(now)
        healthy = counts[ACTIVE]
        interval = self.fleet_interval(cfg, 1)
        rate = 1.0 / interval if interval > 0 else 0.0
        interval_acc = (healthy / rate) if rate > 0 else None
        starved_ratio = min(1.0, sum(d for _, d in self._starved) / window) if window > 0 else 0.0

        warning = None
        if self.slots and healthy == 0:
            warning = WARN_NOT_ENOUGH_ACCOUNTS
        elif interval_acc is not None and interval_acc < cfg.mean_pause and cfg.pause_max_seconds > 0:
            warning = WARN_NOT_ENOUGH_ACCOUNTS
        elif starved_ratio > STARVED_WARN_RATIO:
            warning = WARN_NOT_ENOUGH_ACCOUNTS

        return {
            "schema": 1,
            "ts": time.time(),
            "mode": cfg.mode,
            "target_fleet_rate": cfg.target_fleet_rate if cfg.target_fleet_rate else "auto",
            "fleet_rate": round(rate, 3),                 # T_fleet, запросов/с
            "fleet_interval": round(interval, 3),
            "interval_per_account": None if interval_acc is None else round(interval_acc, 2),
            "min_pause_seconds": [cfg.pause_min_seconds, cfg.pause_max_seconds],
            "accounts_total": len(self.slots),
            "accounts_healthy": healthy,
            "states": counts,
            "starved_ratio": round(starved_ratio, 3),
            "warning": warning,
            "overlap_ewma": None if self._ewma_overlap is None else round(self._ewma_overlap, 3),
            "totals": dict(self.totals),
            "accounts": [
                {
                    "name": s.name,
                    "state": s.health.effective_state(now),
                    "since_s": round(now - s.health.since, 1),
                    "ready_in_s": round(max(0.0, s.not_before - now), 1),
                    "last_error": s.health.last_error,
                }
                for s in self.slots
            ],
        }

    @staticmethod
    def _write_snapshot(path: Path, snap: dict):
        tmp = path.with_name(path.name + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    async def stats_loop(self, log, every: float = 10.0, window: float = 30.0,
                         snapshot_path: Path | None = None, warn_every: float = 300.0):
        while True:
            await asyncio.sleep(every)
            now = self._clock()
            snap = self.snapshot(window)
            uniq = sum(n for _, n in self._sent) / window
            polls = len(self._polls) / window
            counts = snap["states"]
            log.info(
                "[stream] в_буфер=%.1f/с опросов=%.2f/с T_парка=%.2f/с интервал_аккаунта=%ss "
                "доля_дублей(ewma)=%s аккаунтов: active=%d cooldown=%d degraded=%d auth_failed=%d "
                "blocked_node=%d disabled=%d",
                uniq, polls, snap["fleet_rate"], snap["interval_per_account"],
                "n/a" if self._ewma_overlap is None else f"{self._ewma_overlap:.2f}",
                counts[ACTIVE], counts["cooldown"], counts[DEGRADED], counts[AUTH_FAILED],
                counts[BLOCKED_NODE], counts[DISABLED],
            )
            warn = snap["warning"]
            if warn and (warn != self._last_warning or self._warn_logged_at is None
                         or now - self._warn_logged_at >= warn_every):
                log.warning(
                    "[stream] не хватает аккаунтов для целевой скорости: здоровых %d, T_парка=%.2f/с, "
                    "интервал на аккаунт %ss при паузе %g–%g с (простой без готовых аккаунтов %.0f%%)",
                    snap["accounts_healthy"], snap["fleet_rate"], snap["interval_per_account"],
                    snap["min_pause_seconds"][0], snap["min_pause_seconds"][1],
                    snap["starved_ratio"] * 100,
                )
                self._warn_logged_at = now
            elif not warn and self._last_warning:
                log.info("[stream] парку снова хватает аккаунтов для целевой скорости")
            self._last_warning = warn

            if snapshot_path is not None:
                try:
                    self._write_snapshot(snapshot_path, snap)
                except OSError as e:
                    log.warning("[stream] не смог записать %s: %s", snapshot_path, e)