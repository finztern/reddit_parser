import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


# ---------------------------------------------------------------- #
#  Пересоздаваемый ThreadPoolExecutor
# ---------------------------------------------------------------- #

class ExecutorHandle:
    """Держит ТЕКУЩИЙ ThreadPoolExecutor и умеет пересоздавать его на
    лету (см. health_report_loop ниже). Нужно, потому что
    asyncio.wait_for() в http_client.py не может принудительно прервать
    физический поток curl_cffi: если прокси/DNS зависают навсегда, поток
    остаётся "занятым" бесконечно, и за недели работы фиксированный пул
    исчерпывается. Единственный способ "вылечиться" без рестарта
    процесса — списать пул целиком и продолжить с новым.

    Воркеры получают этот handle, а НЕ голый ThreadPoolExecutor, и на
    каждый HTTP-запрос резолвят .current заново. Присваивание .current —
    атомарная операция; читается/пишется только из корутин в потоке
    event loop, поэтому Lock не нужен."""

    def __init__(self, factory: Callable[[], ThreadPoolExecutor], max_workers: int):
        self._factory = factory
        self.max_workers = max_workers
        self.current = factory()
        self.swaps = 0

    def swap(self) -> ThreadPoolExecutor:
        """Создаёт новый пул, переключает .current на него и гасит старый."""
        old = self.current
        self.current = self._factory()
        self.swaps += 1
        # cancel_futures=True отменяет только ещё не стартовавшие задачи.
        # Уже зависшие потоки остаются висеть как ОС-потоки до (если)
        # естественного завершения.
        old.shutdown(wait=False, cancel_futures=True)
        return old

    def shutdown(self, **kwargs):
        self.current.shutdown(**kwargs)


# ---------------------------------------------------------------- #
#  Здоровье executor'а: занятость + учёт свопов/утечек
# ---------------------------------------------------------------- #

class ExecutorHealth:
    """Отслеживает реальную занятость ТЕКУЩЕГО пула.

    on_submit()/on_future_done() вызываются только из корутин/колбэков в
    потоке event loop, поэтому Lock не нужен.

    ИСПРАВЛЕНО: у каждого пула своё "поколение" (generation).
    on_submit() возвращает поколение, в котором задача поставлена, а
    on_future_done(gen) игнорирует завершения задач СТАРЫХ пулов. Раньше
    после свопа счётчики обнулялись, но зависшие потоки старого пула,
    завершаясь позже, увеличивали completed уже НОВОГО пула — active
    уходил в минус и маскировал реальную занятость, из-за чего следующий
    своп мог не сработать никогда."""

    def __init__(self):
        self.generation = 0
        self.submitted = 0
        self.completed = 0
        self.timed_out_waits = 0
        # Суммарно потоков, списанных вместе со старыми пулами при свопах.
        self.leaked_total = 0
        # time.monotonic() последнего свопа, 0.0 — свопов ещё не было.
        self.last_swap_at = 0.0

    @property
    def active(self) -> int:
        return max(0, self.submitted - self.completed)

    def on_submit(self) -> int:
        self.submitted += 1
        return self.generation

    def on_future_done(self, generation: int | None = None, _future=None):
        if generation is not None and generation != self.generation:
            return  # поток старого, уже списанного пула
        self.completed += 1

    def on_wait_timeout(self):
        self.timed_out_waits += 1

    def register_swap(self, leaked_now: int):
        """Вызывается сразу после ExecutorHandle.swap(): начинает новое
        поколение и обнуляет счётчики под НОВЫЙ пул."""
        self.leaked_total += leaked_now
        self.generation += 1
        self.submitted = 0
        self.completed = 0
        self.last_swap_at = time.monotonic()

    def snapshot(self, max_workers: int | None = None, swaps: int | None = None) -> str:
        parts = [
            f"active={self.active}",
            f"submitted={self.submitted}",
            f"completed={self.completed}",
            f"timed_out_waits={self.timed_out_waits}",
            f"leaked_total={self.leaked_total}",
            f"swaps={swaps if swaps is not None else self.generation}",
        ]
        if max_workers is not None:
            parts.append(f"pool_size={max_workers}")
        return " ".join(parts)


async def health_report_loop(
    health: ExecutorHealth,
    handle: ExecutorHandle,
    interval_seconds: float,
    log,
    swap_threshold: float = 0.9,
    swap_cooldown_seconds: float = 300.0,
    fatal_leak_multiplier: float = 10.0,
):
    """Раз в interval_seconds логирует занятость http_executor.

    Если active >= swap_threshold * max_workers — пул пересоздаётся
    через handle.swap(). Два предохранителя:

    - swap_cooldown_seconds — не свопать чаще, чем раз в этот интервал
      (иначе мёртвая VPN-нода приводила бы к штамповке пулов подряд);
    - fatal_leak_multiplier — если суммарная утечка выросла настолько,
      что свопы не помогают, процесс завершается (SystemExit), чтобы
      docker (`restart: unless-stopped`) перезапустил его чисто."""
    while True:
        await asyncio.sleep(interval_seconds)
        active = health.active
        max_workers = handle.max_workers

        if not max_workers or active < max_workers * swap_threshold:
            log.info("[health] http_executor: %s", health.snapshot(max_workers, handle.swaps))
            continue

        since_last_swap = (
            time.monotonic() - health.last_swap_at
            if health.last_swap_at
            else swap_cooldown_seconds  # свопов ещё не было — cooldown не блокирует первый
        )
        if since_last_swap < swap_cooldown_seconds:
            log.warning(
                "[health] http_executor почти исчерпан (%s) ⚠, но с последнего свопа прошло "
                "только %.0fs из %.0fs cooldown — жду, чтобы не штамповать пулы подряд",
                health.snapshot(max_workers, handle.swaps), since_last_swap, swap_cooldown_seconds,
            )
            continue

        leaked_now = active
        handle.swap()
        health.register_swap(leaked_now)
        log.error(
            "[health] http_executor пересоздан (своп #%d): %d поток(ов) старого пула считались "
            "занятыми (вероятно, зависли навсегда на мёртвых прокси/DNS) — новый пул чист. "
            "Суммарно утекло потоков за время жизни процесса: %d. Если это повторяется регулярно — "
            "разберись с конкретной VPN-нодой/аккаунтом (python3 scripts/check_nodes.py).",
            handle.swaps, leaked_now, health.leaked_total,
        )

        if health.leaked_total >= max_workers * fatal_leak_multiplier:
            log.critical(
                "[health] утечка потоков не самоограничивается (leaked_total=%d >= "
                "%.0fx pool_size=%d) — своп больше не помогает, останавливаю процесс, "
                "чтобы docker перезапустил его чисто (restart: unless-stopped)",
                health.leaked_total, fatal_leak_multiplier, max_workers,
            )
            raise SystemExit(1)
