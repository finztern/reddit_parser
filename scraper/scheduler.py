"""
Единый планировщик слотов опроса.

Идея: аккаунты НЕ опрашивают Reddit каждый по своему таймеру (это давало
пачки запросов + огромное число дублей на одном и том же листинге r/all).
Вместо этого планировщик раз в `interval` секунд (с джиттером) выдаёт
"слот" одному аккаунту — тому из готовых, который ДОЛЬШЕ ВСЕХ не
опрашивался (остальные отдыхают дольше). Аккаунт, у которого по его
собственным X-Ratelimit-* ещё не наступил not_before, пропускается — свой
бюджет каждый аккаунт считает независимо.

Опрос идёт ПО СПРОСУ: слоты выдаются, только пока Sender.demand() говорит,
что буфер отправки не полон (DEMAND_NONE — ждём). Если очереди пайплайна не
хватает данных (DEMAND_URGENT) — слоты идут с минимальным интервалом,
подключая больше аккаунтов.

`interval` подстраивается по доле дублей на первой странице ответа:
  - мало дублей (почти пропуск между опросами) -> опрашиваем чаще;
  - много дублей (опрашиваем впустую)          -> реже.
"""

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class PollOutcome:
    page_len: int         # сколько комментариев на первой странице
    dupes: int            # из них уже виденных (до claim)
    pages: int            # сколько страниц выкачано
    overlap_found: bool   # дошли ли до уже виденных


class AccountSlot:
    __slots__ = ("name", "not_before", "busy", "alive", "queue", "last_poll", "after_pause")

    def __init__(self, name: str):
        self.name = name
        self.not_before = 0.0
        self.last_poll = 0.0        # monotonic последней выдачи слота (0 = ещё не опрашивался)
        self.after_pause = False    # слот выдан после простоя по спросу — доле дублей верить нельзя
        self.busy = False
        self.alive = True
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1)


class PollScheduler:
    def __init__(self, get, demand=None):
        self._get = get  # ConfigStore.get — конфиг читается на лету
        self._demand = demand  # Sender.demand: 0 — не опрашивать, 1 — обычно, 2 — срочно
        self._idle = False     # был простой из-за полного буфера
        self.slots: list[AccountSlot] = []
        self.interval = float(get("stream_start_interval_seconds", 1.5))
        self._ewma_overlap: float | None = None
        self._sent = deque()    # (ts, n) отправленных уникальных
        self._polls = deque()   # ts опросов

    # ---- регистрация ----
    def register(self, name: str) -> AccountSlot:
        slot = AccountSlot(name)
        self.slots.append(slot)
        return slot

    def unregister(self, slot: AccountSlot):
        slot.alive = False
        if slot in self.slots:
            self.slots.remove(slot)

    # ---- вызывается воркером после каждого цикла (в т.ч. ошибочного) ----
    def release(self, slot: AccountSlot, outcome: PollOutcome | None,
                cooldown: float, sent: int):
        now = time.monotonic()
        slot.busy = False
        slot.not_before = now + max(0.0, cooldown)
        self._polls.append(now)
        if sent:
            self._sent.append((now, sent))
        # после простоя (полный буфер) первая страница не пересекается с
        # виденным не из-за пропуска, а из-за паузы — не подстраиваем интервал
        if outcome is not None and not slot.after_pause:
            self._adapt(outcome)
        slot.after_pause = False

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

    def _pick(self, now: float) -> AccountSlot | None:
        """Среди готовых аккаунтов — тот, что дольше всех не опрашивался."""
        best = None
        for s in self.slots:
            if s.alive and not s.busy and s.not_before <= now:
                if best is None or s.last_poll < best.last_poll:
                    best = s
        return best

    async def run(self):
        while True:
            mode = self._demand() if self._demand else 1
            if mode <= 0:
                self._idle = True
                await asyncio.sleep(0.05)
                continue
            now = time.monotonic()
            slot = self._pick(now)
            if slot is None:
                await asyncio.sleep(0.05)
                continue
            slot.busy = True
            slot.last_poll = now
            slot.after_pause = self._idle
            self._idle = False
            slot.queue.put_nowait(True)
            interval = self.interval
            if mode >= 2:  # очереди пайплайна не хватает данных — максимальный темп
                interval = float(self._get("stream_min_interval_seconds", 0.4))
            j = self._get("stream_jitter_ratio", 0.2)
            await asyncio.sleep(interval * random.uniform(1 - j, 1 + j))

    async def stats_loop(self, log, every: float = 10.0, window: float = 30.0):
        while True:
            await asyncio.sleep(every)
            now = time.monotonic()
            while self._sent and now - self._sent[0][0] > window:
                self._sent.popleft()
            while self._polls and now - self._polls[0] > window:
                self._polls.popleft()
            uniq = sum(n for _, n in self._sent) / window
            polls = len(self._polls) / window
            ready = sum(1 for s in self.slots if s.not_before <= now)
            log.info(
                "[stream] в_буфер=%.1f/с опросов=%.2f/с интервал_слота=%.2fs "
                "доля_дублей(ewma)=%s аккаунтов_готово=%d/%d",
                uniq, polls, self.interval,
                "n/a" if self._ewma_overlap is None else f"{self._ewma_overlap:.2f}",
                ready, len(self.slots),
            )
