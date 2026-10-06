import asyncio
import random

import aiohttp

from .config import ConfigStore, CookieFileWatcher
from .constants import BASE_DIR, DEFAULT_CONNECT_TIMEOUT_SECONDS, PROXY_HOST, log
from .health import ExecutorHandle, ExecutorHealth
from .http_client import CurlSessionHandle, fetch_comments
from .pipeline import build_payload
from .scheduler import PollOutcome, PollScheduler
from .sender import Sender
from .state import BackoffState, SeenCache

# Как часто проверять появление/починку cookies-файла, пока его нет
# (гостевой слот ещё не харвестился) или он не читается.
COOKIE_WAIT_SECONDS = 60

# Кулдаун при пустом листинге (200 + пустой Listing на первой странице):
# EMPTY_BASE * 2^(streak-1), не больше EMPTY_MAX. После EMPTY_SWAP_AFTER
# пустых подряд пересоздаём curl-сессию (свежее соединение).
EMPTY_BASE_COOLDOWN = 3.0
EMPTY_MAX_COOLDOWN = 60.0
EMPTY_SWAP_AFTER = 3


def _fullname(c: dict) -> str:
    return c.get("name") or f"t1_{c.get('id')}"


def rl_cooldown(ratelimit: dict | None, margin: float, max_interval: float) -> float:
    """Собственный бюджет аккаунта: через сколько секунд он снова имеет
    право делать запрос, чтобы равномерно растянуть remaining на reset."""
    if not ratelimit:
        return 0.0
    remaining, reset = ratelimit["remaining"], ratelimit["reset"]
    if remaining <= 1:
        v = reset + 1.0
    elif reset > 0:
        v = reset / (remaining * margin)
    else:
        v = 0.0
    return min(v, max_interval)


async def account_worker(
    account: dict,
    config: ConfigStore,
    seen: SeenCache,
    scheduler: PollScheduler,
    sender: Sender,
    http_executor: ExecutorHandle,
    executor_health: ExecutorHealth | None = None,
):
    name = account["name"]
    cookie_file = BASE_DIR / account["cookie_file"]
    proxy_url = f"http://{PROXY_HOST}:{account['proxy_port']}"

    # Раньше при отсутствии файла воркер делал return и больше НЕ
    # запускался (а в main.py завершение любого воркера останавливало весь
    # процесс). Теперь ждём: гостевой слот можно включить до первого
    # харвеста cookies — воркер стартует, как только файл появится.
    if not cookie_file.exists():
        log.warning("[%s] Файл с cookies не найден: %s — жду его появления (проверка раз в %ds)",
                    name, cookie_file, COOKIE_WAIT_SECONDS)
        while not cookie_file.exists():
            await asyncio.sleep(COOKIE_WAIT_SECONDS)

    cookie_watcher = CookieFileWatcher(
        cookie_file, min_check_interval=config.get("cookie_reload_check_interval_seconds", 30)
    )
    cookies = cookie_watcher.load_if_changed(force=True)
    while cookies is None:
        log.warning("[%s] Не удалось прочитать cookies из %s — повторю через %ds",
                    name, cookie_file, COOKIE_WAIT_SECONDS)
        await asyncio.sleep(COOKIE_WAIT_SECONDS)
        cookies = cookie_watcher.load_if_changed(force=True)

    user_agent = account.get("user_agent") or config.get("user_agent", "Mozilla/5.0")
    impersonate = account.get("impersonate") or config.get("impersonate", "chrome")
    # NB: реальные запросы к Reddit идут через curl_cffi с impersonate (он
    # сам ставит UA/заголовки, соответствующие TLS-отпечатку). Эти headers
    # живут только в aiohttp-сессии (хранилище cookie_jar).
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    curl_session = CurlSessionHandle(
        impersonate=impersonate, proxies={"http": proxy_url, "https": proxy_url}
    )
    backoff = BackoffState(
        base_seconds=config.get("base_backoff_seconds", 5),
        max_seconds=config.get("max_backoff_seconds", 300),
        max_auth_errors=config.get("max_consecutive_auth_errors", 5),
    )
    auth_dead = False  # cookies отвергнуты — ждём обновления файла
    empty_streak = 0   # подряд пустых листингов (200, children=[]) на 1-й странице

    slot = scheduler.register(name)
    log.info("[%s] Старт (прокси %s, impersonate=%s) — жду слоты планировщика", name, proxy_url, impersonate)

    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
            while True:
                await slot.queue.get()  # слот выдан планировщиком

                outcome: PollOutcome | None = None
                cooldown = 0.0
                queued = 0
                try:
                    max_age = config.get("max_age_seconds", 120)
                    max_pages = config.get("pagination_max_pages", 5)
                    pg_min = config.get("pagination_delay_min_seconds", 0.1)
                    pg_max = config.get("pagination_delay_max_seconds", 0.4)
                    margin = config.get("ratelimit_safety_margin", 0.85)
                    rl_max = config.get("ratelimit_max_interval_seconds", 900)
                    timeout = config.get("request_timeout_seconds", 10)
                    connect_timeout = config.get("connect_timeout_seconds", DEFAULT_CONNECT_TIMEOUT_SECONDS)
                    base_url = config.get("reddit_base_url", "https://www.reddit.com")
                    fetch_limit = min(100, config.get("fetch_limit", 100))
                    subs_joined = "+".join(config.get("stream_subs", ["all"]))

                    cookie_watcher.min_check_interval = config.get("cookie_reload_check_interval_seconds", 30)
                    new_cookies = cookie_watcher.load_if_changed()
                    if new_cookies is not None:
                        session.cookie_jar.clear()
                        session.cookie_jar.update_cookies(new_cookies)
                        # curl-сессия копит Set-Cookie от Reddit — сбрасываем,
                        # чтобы старые значения не конфликтовали со свежими.
                        curl_session.swap()
                        log.info("[%s] cookies обновлены из %s (%d шт.)", name, cookie_file, len(new_cookies))
                        if auth_dead or backoff.consecutive_auth_errors:
                            backoff.consecutive_auth_errors = 0
                            auth_dead = False
                            log.info("[%s] новые cookies — сбрасываю счётчик auth-ошибок, возобновляю опрос", name)

                    # Скорость отправки теперь ведёт Sender (ровными порциями)
                    # под потолком QueueGovernor; воркер только наполняет буфер.

                    # Остановка пагинации: как только на странице встретился
                    # уже виденный комментарий — догнали, дальше листать не надо.
                    stats = {"pages": 0, "first_len": 0, "first_dupes": 0, "overlap": False}

                    def stop_when(page: list[dict]) -> bool:
                        stats["pages"] += 1
                        dupes = sum(1 for c in page if seen.contains(_fullname(c)))
                        if stats["pages"] == 1:
                            stats["first_len"] = len(page)
                            stats["first_dupes"] = dupes
                        if dupes:
                            stats["overlap"] = True
                            return True
                        return False

                    result = await fetch_comments(
                        session, base_url, subs_joined, fetch_limit, curl_session, timeout, name,
                        http_executor, max_age, max_pages, connect_timeout,
                        pg_min, pg_max, executor_health, stop_when=stop_when,
                    )

                    # ---- ошибки: бэкофф только у этого аккаунта (cooldown),
                    #      остальные продолжают получать слоты ----
                    if result.error_kind == "rate_or_server":
                        cooldown = backoff.register_rate_or_server_error()
                        if result.retry_after:
                            cooldown = max(cooldown, result.retry_after)
                        log.warning("[%s] %s — аккаунт отдыхает %.1fs (подряд: %d)",
                                    name, result.status, cooldown, backoff.consecutive_errors)
                        continue

                    if result.error_kind == "auth":
                        cooldown, should_stop = backoff.register_auth_error()
                        if should_stop:
                            # Пауза с редкими пробами; новые cookies (hot-reload
                            # выше) сами возобновляют работу.
                            cooldown = max(cooldown, float(config.get("auth_dead_cooldown_seconds", 120)))
                            if not auth_dead:
                                auth_dead = True
                                log.error(
                                    "[%s] %s подряд %d раз — cookies протухли/бан. Воркер на паузе: "
                                    "жду обновления %s (проба раз в ~%.0fs)",
                                    name, result.status, backoff.consecutive_auth_errors, cookie_file, cooldown,
                                )
                        else:
                            log.warning("[%s] %s — отдых %.1fs (auth-ошибок %d/%d)", name, result.status,
                                        cooldown, backoff.consecutive_auth_errors, backoff.max_auth_errors)
                        continue

                    if result.error_kind == "empty":
                        # 200 + пустой Listing на первой странице: мягкий
                        # троттлинг/пустой ответ edge. Это НЕ успех — аккаунт
                        # отдыхает с нарастающим кулдауном, чтобы не жечь слоты.
                        empty_streak += 1
                        cooldown = min(EMPTY_MAX_COOLDOWN,
                                       EMPTY_BASE_COOLDOWN * (2 ** (empty_streak - 1)))
                        cooldown *= random.uniform(0.8, 1.3)
                        # если Reddit прислал X-Ratelimit-* — уважаем и его
                        cooldown = max(cooldown, rl_cooldown(result.ratelimit, margin, rl_max))
                        if empty_streak >= EMPTY_SWAP_AFTER:
                            curl_session.swap()  # свежее соединение/keep-alive
                        log.warning("[%s] пустой листинг подряд %d — отдых %.1fs",
                                    name, empty_streak, cooldown)
                        continue

                    if result.error_kind == "network":
                        cooldown = min(backoff.register_rate_or_server_error(), 30.0)
                        continue

                    # ---- успех ----
                    backoff.register_success()
                    auth_dead = False
                    empty_streak = 0
                    comments = result.comments

                    payloads = []
                    for c in comments:
                        p = build_payload(c)
                        if p is None or p["_age_seconds"] > max_age:
                            continue
                        payloads.append(p)
                    # Старые первыми: буфер Sender'а — FIFO, самые старые
                    # должны уйти раньше, пока не протухли по max_age.
                    payloads.sort(key=lambda p: p["_age_seconds"], reverse=True)

                    to_queue: list[dict] = []
                    dropped_dup = 0
                    for p in payloads:
                        if not await seen.try_claim(p["external_id"]):
                            dropped_dup += 1
                            continue
                        to_queue.append(p)

                    # Отправку (confirm/release в SeenCache) делает Sender.
                    queued = await sender.put_many(to_queue)

                    outcome = PollOutcome(
                        page_len=stats["first_len"], dupes=stats["first_dupes"],
                        pages=result.pages_fetched, overlap_found=stats["overlap"],
                    )
                    cooldown = rl_cooldown(result.ratelimit, margin, rl_max)

                    log.info(
                        "[%s] r/%s стр=%d получено=%d дубли_стр1=%d/%d в_буфер=%d дубли=%d "
                        "буфер=%d догнали=%s cooldown=%.1fs",
                        name, subs_joined, result.pages_fetched, len(comments),
                        stats["first_dupes"], stats["first_len"], queued, dropped_dup,
                        sender.backlog, stats["overlap"], cooldown,
                    )
                finally:
                    scheduler.release(slot, outcome, cooldown, queued)
    finally:
        scheduler.unregister(slot)


async def supervised(
    coro_fn, *args, name: str = "worker",
    base_backoff: float = 2.0, max_backoff: float = 60.0, **kwargs,
):
    attempt = 0
    while True:
        try:
            await coro_fn(*args, **kwargs)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            attempt += 1
            delay = min(max_backoff, base_backoff * (2 ** (attempt - 1)))
            log.exception("[%s] воркер упал (попытка %d), рестарт через %.1fs", name, attempt, delay)
            await asyncio.sleep(delay)
