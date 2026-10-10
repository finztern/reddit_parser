"""
Sender — отправка в /store_items по состоянию очереди пайплайна (bpipe).

Цель: реплики пайплайна всегда загружены (bpipe_inflight == inflight_target,
по умолчанию 128), а очередь ожидания bpipe_queue всегда готова их
накормить (>= queue_target, по умолчанию = inflight_target). Тогда в момент,
когда реплики закончат обработку, следующий батч уже лежит в очереди.

Как это работает:

  * Воркеры только КЛАДУТ комментарии в буфер (put_many). Id остаётся в
    SeenCache._pending, пока комментарий в буфере: другие аккаунты видят его
    как дубль, "догнали" в пагинации работает.
  * PollScheduler спрашивает demand(): опрашивать Reddit нужно, только пока
    буфер меньше `buffer_target_factor * batch`. Буфер полон — аккаунты
    отдыхают (не жгут X-Ratelimit-бюджет и не копят старьё).
  * Раз в `poll_interval_seconds` читается GET queue_control.url. Если
    bpipe_queue < queue_target — уходит батч (send_batch_size, по умолчанию
    = inflight_target) из САМЫХ СВЕЖИХ комментариев буфера, в порядке
    свежести (самые свежие первыми). Неполный батч шлётся, только если
    очередь почти пуста (< low_queue_fraction * queue_target) или самый
    старый элемент уже "созрел" (flush_age_fraction * max_age).
  * Комментарии старше max_age перед отправкой выбрасываются; при
    переполнении буфера (buffer_max_factor * batch) вытесняются самые старые.
  * Если /queue молчит дольше stale_seconds — fallback: батч раз в
    batch / target_rate_per_second секунд.
"""

import asyncio
import time

import aiohttp

from .constants import QUEUE_CONTROL_URL_OVERRIDE, SERVER_HARD_BATCH_LIMIT, STORE_ENDPOINT_OVERRIDE
from .pipeline import send_batch_to_store
from .state import SeenCache

# demand(): что нужно планировщику
DEMAND_NONE = 0     # буфер полон — Reddit не опрашиваем
DEMAND_NORMAL = 1   # буфер не полон — опрашиваем в обычном темпе
DEMAND_URGENT = 2   # буфер меньше батча, а пайплайну нужны данные — темп максимальный


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Sender:
    def __init__(self, config, seen: SeenCache, session: aiohttp.ClientSession, log):
        self.config = config
        self.seen = seen
        self.session = session
        self.log = log

        self._buf: list[dict] = []
        self._status: dict | None = None     # последний ответ /queue
        self._last_ok: float | None = None   # monotonic последнего успешного /queue
        self._started = time.monotonic()
        self._fallback = False
        self._hold_until = 0.0               # не слать раньше (после отправки/ошибки)
        self._last_send = 0.0
        self._starving = False               # очереди нужны данные, а в буфере меньше батча

        # окно статистики
        self._w_batches = 0
        self._w_sent = 0
        self._w_partial = 0
        self._w_failed = 0
        self._w_expired = 0
        self._w_evicted = 0
        self._w_enq = 0
        self._w_starved_ticks = 0
        self._w_ticks = 0

    # ---- конфиг ----
    def _qc(self) -> dict:
        return self.config.get("queue_control", {}) or {}

    def _inflight_target(self) -> int:
        return max(1, int(self._qc().get("inflight_target", 128)))

    def _batch(self) -> int:
        b = int(self._qc().get("send_batch_size", self._inflight_target()))
        return max(1, min(b, SERVER_HARD_BATCH_LIMIT))

    def _queue_target(self) -> int:
        return max(1, int(self._qc().get("queue_target", self._inflight_target())))

    def _fill_target(self) -> int:
        return max(self._batch(), int(self._batch() * float(self._qc().get("buffer_target_factor", 1.25))))

    def _buffer_max(self) -> int:
        return max(self._fill_target(), int(self._batch() * float(self._qc().get("buffer_max_factor", 3.0))))

    def _url(self) -> str | None:
        return QUEUE_CONTROL_URL_OVERRIDE or self._qc().get("url")

    # ---- публичное API ----
    @property
    def backlog(self) -> int:
        return len(self._buf)

    def demand(self) -> int:
        """Нужен ли планировщику новый опрос Reddit прямо сейчас."""
        n = len(self._buf)
        if n >= self._fill_target():
            return DEMAND_NONE
        if self._starving and n < self._batch():
            return DEMAND_URGENT
        return DEMAND_NORMAL

    async def put_many(self, items: list[dict]) -> int:
        """Кладёт уже claim'нутые (seen.try_claim) payload'ы в буфер.
        При переполнении вытесняет самые старые."""
        if not items:
            return 0
        self._buf.extend(items)
        self._w_enq += len(items)

        cap = self._buffer_max()
        if len(self._buf) > cap:
            self._buf.sort(key=lambda p: p["_created_utc"], reverse=True)  # свежие первыми
            evicted = self._buf[cap:]
            del self._buf[cap:]
            for p in evicted:
                await self.seen.release(p["external_id"])
            self._w_evicted += len(evicted)
        return len(items)

    # ---- /queue ----
    async def _fetch(self, url: str, timeout: float) -> dict | None:
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    self.log.warning("[send] %s ответил %s", url, resp.status)
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log.warning("[send] не удалось прочитать %s: %s", url, e)
            return None

    # ---- буфер ----
    async def _expire(self):
        max_age = float(self.config.get("max_age_seconds", 15))
        now_wall = time.time()
        keep = []
        for p in self._buf:
            if now_wall - p["_created_utc"] > max_age:
                await self.seen.release(p["external_id"])
                self._w_expired += 1
            else:
                keep.append(p)
        self._buf = keep

    def _oldest_age(self) -> float:
        if not self._buf:
            return 0.0
        return time.time() - min(p["_created_utc"] for p in self._buf)

    def _take_freshest(self, n: int) -> list[dict]:
        self._buf.sort(key=lambda p: p["_created_utc"], reverse=True)  # самые свежие первыми
        batch = self._buf[:n]
        del self._buf[:n]
        return batch

    async def _send(self, items: list[dict]) -> int:
        endpoint = STORE_ENDPOINT_OVERRIDE or self.config.get("store_endpoint")
        if not endpoint:
            self.log.error("[send] store_endpoint не задан — выбрасываю %d элементов", len(items))
            for p in items:
                await self.seen.release(p["external_id"])
            return 0

        max_age = float(self.config.get("max_age_seconds", 15))
        sent = 0
        done: set[str] = set()
        retry: list[dict] = []
        try:
            flags = await send_batch_to_store(
                self.session, endpoint, items, "send", int(self.config.get("batch_max_items", 500))
            )
            now_wall = time.time()
            for p, ok in zip(items, flags):
                done.add(p["external_id"])
                if ok:
                    await self.seen.confirm(p["external_id"])
                    sent += 1
                elif now_wall - p["_created_utc"] <= max_age:
                    retry.append(p)          # вернём в буфер, пока не протухло
                    self._w_failed += 1
                else:
                    await self.seen.release(p["external_id"])
                    self._w_failed += 1
        finally:
            for p in items:
                if p["external_id"] not in done:
                    await self.seen.release(p["external_id"])
        if retry:
            self._buf.extend(retry)
            self._hold_until = max(self._hold_until, time.monotonic() + 1.0)  # не долбим упавший пайплайн
        return sent

    async def _flush(self, n: int, partial: bool):
        items = self._take_freshest(n)
        if not items:
            return
        sent = await self._send(items)
        self._w_batches += 1
        self._w_sent += sent
        if partial:
            self._w_partial += 1
        self._last_send = time.monotonic()
        # /queue ещё не отразит отправленное — учитываем сами до следующего опроса
        if self._status is not None and not self._fallback:
            self._status["bpipe_queue"] = (_num(self._status.get("bpipe_queue"), 0.0) or 0.0) + sent
        self._hold_until = max(self._hold_until, self._last_send + float(self._qc().get("min_send_gap_seconds", 0.4)))

    # ---- решение ----
    async def _tick(self):
        c = self._qc()
        now = time.monotonic()
        await self._expire()
        n = len(self._buf)
        batch = self._batch()
        qt = self._queue_target()
        self._w_ticks += 1

        url = self._url()
        live = bool(c.get("enabled", False)) and bool(url)
        status = None
        if live:
            status = await self._fetch(url, float(c.get("request_timeout_seconds", 2.0)))
            if status is not None:
                self._status = status
                self._last_ok = now
                if self._fallback:
                    self._fallback = False
                    self.log.info("[send] /queue снова доступна — работаю по очереди")
            else:
                since = self._last_ok if self._last_ok is not None else self._started
                if not self._fallback and now - since > float(c.get("stale_seconds", 30)):
                    self._fallback = True
                    self.log.warning("[send] /queue недоступна > %.0fs — fallback: батч раз в %.2fs "
                                     "(target_rate_per_second=%s)", float(c.get("stale_seconds", 30)),
                                     batch / max(1.0, float(self.config.get("target_rate_per_second", 80))),
                                     self.config.get("target_rate_per_second", 80))

        flush_age = float(self.config.get("max_age_seconds", 15)) * float(c.get("flush_age_fraction", 0.6))

        # ---------- fallback / очередь выключена: ровный темп ----------
        if not live or self._fallback or (status is None and self._status is None):
            self._starving = False
            gap = batch / max(1.0, float(self.config.get("target_rate_per_second", 80)))
            if now >= self._hold_until and now - self._last_send >= gap and n:
                if n >= batch or self._oldest_age() >= flush_age:
                    await self._flush(batch, partial=n < batch)
            return

        st = self._status
        if status is None:
            return  # разовый сбой опроса: решение примем на следующем тике

        bq = _num(st.get("bpipe_queue"))
        if bq is None:
            return
        bmax = _num(st.get("bpipe_max"))
        panic = bool(st.get("degraded")) or bool(bmax and bq >= bmax * float(c.get("panic_fraction", 0.9)))
        action = str(st.get("action", "")).lower()
        slow = bool(c.get("respect_server_action", True)) and any(w in action for w in ("slow", "pause", "stop"))

        starving = bq < qt * float(c.get("low_queue_fraction", 0.5))
        # планировщику нужен максимальный темп, только если очереди реально не хватает данных
        self._starving = bq < qt and n < batch and not panic and not slow
        if self._starving:
            self._w_starved_ticks += 1

        if panic or slow or now < self._hold_until or bq >= qt or n == 0:
            return

        if n >= batch:
            await self._flush(batch, partial=False)
        elif starving or self._oldest_age() >= flush_age:
            # очередь почти пуста (или данные уже созрели) — шлём что есть, не простаиваем
            await self._flush(n, partial=True)

    def _log_window(self, elapsed: float):
        e = max(elapsed, 1e-6)
        st = self._status or {}
        self.log.info(
            "[send] отправлено=%.1f/с батчей=%d (неполных %d) приток=%.1f/с буфер=%d | "
            "bpipe: очередь=%s inflight=%s/%d | протухло=%d вытеснено=%d не_подтв=%d "
            "тиков_без_данных=%d/%d режим=%s",
            self._w_sent / e, self._w_batches, self._w_partial, self._w_enq / e, len(self._buf),
            st.get("bpipe_queue", "n/a"), st.get("bpipe_inflight", "n/a"), self._inflight_target(),
            self._w_expired, self._w_evicted, self._w_failed,
            self._w_starved_ticks, self._w_ticks, "fallback" if self._fallback else "по очереди",
        )
        self._w_batches = self._w_sent = self._w_partial = self._w_failed = 0
        self._w_expired = self._w_evicted = self._w_enq = 0
        self._w_starved_ticks = self._w_ticks = 0

    async def run(self):
        win_start = time.monotonic()
        while True:
            try:
                await self._tick()
            except Exception:
                self.log.exception("[send] ошибка в цикле отправки — продолжаю")
            await asyncio.sleep(max(0.05, float(self._qc().get("poll_interval_seconds", 0.3))))
            now = time.monotonic()
            every = float(self._qc().get("log_every_seconds", 30))
            if now - win_start >= every:
                self._log_window(now - win_start)
                win_start = now
