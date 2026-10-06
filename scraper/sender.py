"""
Sender — сглаживание отправки в /store_items.

Раньше каждый воркер после опроса сразу слал весь свой батч (60-100
комментариев) в пайплайн -> очередь коллектора то пустая, то залпом. Теперь
воркеры только КЛАДУТ комментарии в общий буфер (put_many), а единственный
отправитель каждые `send_tick_seconds` отдаёт из него небольшие порции с
ровной скоростью:

    target = max(приток_ewma * headroom, бэклог / drain_horizon)
    rate   = min(потолок_governor, max(min_rate, target))

То есть шлём примерно с той скоростью, с какой комментарии реально
приходят (пачки от опросов растягиваются во времени), а накопившийся бэклог
сливаем за ~drain_horizon секунд. Потолок (bucket.rate) ведёт QueueGovernor
по состоянию очереди коллектора и работает только как верхняя граница.

Дедуп: id остаётся в SeenCache._pending, пока комментарий лежит в буфере
(другие аккаунты видят его как дубль -> "догнали" работает), confirm() — после
подтверждённой отправки, release() — при ошибке/протухании/переполнении.
"""

import asyncio
import math
import time
from collections import deque

import aiohttp

from .constants import STORE_ENDPOINT_OVERRIDE
from .pipeline import send_batch_to_store
from .state import SeenCache, TokenBucket


class Sender:
    def __init__(self, config, bucket: TokenBucket, seen: SeenCache,
                 session: aiohttp.ClientSession, log):
        self.config = config
        self.bucket = bucket
        self.seen = seen
        self.session = session
        self.log = log

        self._buf: deque[dict] = deque()
        self._supply: float | None = None   # EWMA притока, шт/с
        self._enq_since_tick = 0
        self._limited_until = 0.0
        self.rate_eff = 0.0

        # окно статистики
        self._w_sent = 0
        self._w_failed = 0
        self._w_expired = 0
        self._w_overflow = 0
        self._w_enq = 0

    # ---- конфиг ----
    def _c(self, key, default):
        return self.config.get(key, default)

    # ---- публичное API ----
    @property
    def backlog(self) -> int:
        return len(self._buf)

    def is_cap_limited(self) -> bool:
        """True, если за последние секунды отправку реально сдерживал потолок
        governor'а (а не нехватка комментариев). Только тогда имеет смысл его
        поднимать."""
        return time.monotonic() < self._limited_until

    async def put_many(self, items: list[dict]) -> int:
        """Кладёт уже claim'нутые (seen.try_claim) payload'ы в буфер.
        Возвращает, сколько принято."""
        if not items:
            return 0
        now = time.monotonic()
        for p in items:
            p["_queued_at"] = now
            self._buf.append(p)
        self._enq_since_tick += len(items)
        self._w_enq += len(items)

        max_items = int(self._c("send_buffer_max_items", 5000))
        while len(self._buf) > max_items:
            old = self._buf.popleft()
            await self.seen.release(old["external_id"])
            self._w_overflow += 1
        return len(items)

    # ---- внутреннее ----
    def _update_supply(self, dt: float):
        if dt <= 0:
            return
        inst = self._enq_since_tick / dt
        self._enq_since_tick = 0
        tau = max(0.5, float(self._c("send_supply_tau_seconds", 5.0)))
        a = 1.0 - math.exp(-dt / tau)
        self._supply = inst if self._supply is None else self._supply + a * (inst - self._supply)

    @staticmethod
    def _age(p: dict, now: float) -> float:
        return p["_age_seconds"] + (now - p["_queued_at"])

    async def _sweep_head(self, now: float, max_age: float):
        while self._buf and self._age(self._buf[0], now) > max_age:
            p = self._buf.popleft()
            await self.seen.release(p["external_id"])
            self._w_expired += 1

    async def _tick(self, now: float):
        max_age = float(self._c("max_age_seconds", 120))
        await self._sweep_head(now, max_age)

        backlog = len(self._buf)
        cap = max(0.0, float(self.bucket.rate))
        supply = self._supply or 0.0
        headroom = float(self._c("send_supply_headroom", 1.15))
        horizon = max(0.2, float(self._c("send_drain_horizon_seconds", 1.5)))
        floor = float(self._c("send_min_rate", 2.0))
        burst = float(self._c("send_burst_seconds", 0.3))

        target = max(supply * headroom, backlog / horizon)
        rate = min(cap, max(floor, target))
        self.rate_eff = rate

        if supply * headroom > cap or backlog > cap * 2 * horizon:
            self._limited_until = now + 3.0

        if backlog == 0:
            self.bucket.idle()  # не копим токены "про запас" -> не будет залпа после простоя
            return

        n = self.bucket.take(backlog, rate, burst)
        if n <= 0:
            return

        items: list[dict] = []
        while self._buf and len(items) < n:
            p = self._buf.popleft()
            if self._age(p, now) > max_age:
                await self.seen.release(p["external_id"])
                self._w_expired += 1
                continue
            items.append(p)
        if len(items) < n:
            self.bucket.refund(n - len(items))
        if items:
            await self._send(items)

    async def _send(self, items: list[dict]):
        endpoint = STORE_ENDPOINT_OVERRIDE or self._c("store_endpoint", None)
        batch_max = int(self._c("batch_max_items", 500))
        done: set[str] = set()
        try:
            if not endpoint:
                self.log.error("[send] store_endpoint не задан — выбрасываю %d элементов", len(items))
                return
            flags = await send_batch_to_store(self.session, endpoint, items, "send", batch_max)
            for p, ok in zip(items, flags):
                if ok:
                    await self.seen.confirm(p["external_id"])
                    self._w_sent += 1
                else:
                    await self.seen.release(p["external_id"])
                    self._w_failed += 1
                done.add(p["external_id"])
        finally:
            for p in items:
                if p["external_id"] not in done:
                    await self.seen.release(p["external_id"])

    def _log_window(self, elapsed: float):
        e = max(elapsed, 1e-6)
        self.log.info(
            "[send] отправлено=%.1f/с приток=%.1f/с (ewma %.1f) темп=%.1f/с потолок=%.0f/с "
            "буфер=%d протухло=%d переполнение=%d не_подтв=%d упор_в_потолок=%s",
            self._w_sent / e, self._w_enq / e, self._supply or 0.0, self.rate_eff,
            self.bucket.rate, len(self._buf), self._w_expired, self._w_overflow,
            self._w_failed, self.is_cap_limited(),
        )
        self._w_sent = self._w_failed = self._w_expired = self._w_overflow = self._w_enq = 0

    async def run(self):
        last = time.monotonic()
        win_start = last
        while True:
            tick = max(0.05, float(self._c("send_tick_seconds", 0.25)))
            await asyncio.sleep(tick)
            now = time.monotonic()
            dt = now - last
            last = now
            try:
                self._update_supply(dt)
                await self._tick(now)
            except Exception:
                self.log.exception("[send] ошибка в цикле отправки — продолжаю")
            every = float(self._c("send_log_every_seconds", 10.0))
            if now - win_start >= every:
                self._log_window(now - win_start)
                win_start = now
