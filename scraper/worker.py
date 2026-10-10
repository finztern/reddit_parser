import asyncio
import random
import time

import aiohttp

from . import identity
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

# Пока идёт ротация (заявка pending/working) воркер не опрашивает Reddit
# и лишь раз в столько секунд заглядывает, не закончилась ли она.
ROTATION_POLL_SECONDS = 5.0


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
    sender: Sender,
    seen: SeenCache,
    scheduler: PollScheduler,
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

    # Активный профиль слота после ротации (state/slots/account_N.json)
    # ПЕРЕКРЫВАЕТ accounts.yaml — иначе после рестарта контейнера слот
    # откатился бы на старый отпечаток, а нода и cookies уже новые.
    profile_watcher = identity.SlotProfileWatcher(name)
    prof = profile_watcher.load_if_changed(force=True)
    if prof:
        impersonate = prof["impersonate"]
        user_agent = prof.get("user_agent") or user_agent
        log.info("[%s] профиль из state/slots: impersonate=%s нода=%s (перекрывает accounts.yaml)",
                 name, impersonate, prof.get("node"))

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
    next_request_at = 0.0  # не просить ротацию раньше (wall clock)

    slot = scheduler.register(name)
    log.info("[%s] Старт (прокси %s, impersonate=%s) — жду слоты планировщика", name, proxy_url, impersonate)

    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
            while True:
                await slot.queue.get()  # слот выдан планировщиком

                outcome: PollOutcome | None = None
                cooldown = 0.0
                sent = 0
                try:
                    # ---- ротация идентичности (демон на хосте) ----
                    rot = config.get("rotation", {}) or {}
                    rot_on = bool(rot.get("enabled", False)) and name in (rot.get("slots") or [])
                    force_profile = False
                    if rot_on:
                        req = identity.read_request(name)
                        if req:
                            st = req.get("status")
                            age = time.time() - float(req.get("ts") or 0)
                            if st in ("pending", "working") and age < float(rot.get("request_timeout_seconds", 1800)):
                                cooldown = ROTATION_POLL_SECONDS
                                continue  # ждём демона, Reddit не трогаем
                            identity.clear_request(name)
                            if st in ("done", "rejected"):
                                retry_after = float(req.get("retry_after") or 0)
                                next_request_at = max(next_request_at, retry_after)
                                if st == "done":
                                    empty_streak = 0
                                    backoff.register_success()
                                    auth_dead = False
                                    force_profile = True
                                    log.info("[%s] ротация выполнена — возобновляю опрос с новой идентичностью", name)
                                else:
                                    wait = max(0.0, retry_after - time.time())
                                    log.warning("[%s] ротация отклонена (%s) — продолжаю как есть, "
                                                "следующая заявка не раньше чем через %.0fs",
                                                name, req.get("reason"), wait)
                            else:
                                log.warning("[%s] заявка на ротацию зависла (%s, %.0fs) — снимаю",
                                            name, st, age)

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

                    # Профиль (impersonate) — раньше cookies: демон пишет
                    # state/slots/* ДО замены cookies-файла.
                    new_prof = profile_watcher.load_if_changed(force=force_profile)
                    profile_changed = False
                    if new_prof is not None and new_prof["impersonate"] != impersonate:
                        try:
                            curl_session.set_impersonate(new_prof["impersonate"])
                            log.info("[%s] impersonate %s -> %s (нода %s)", name, impersonate,
                                     new_prof["impersonate"], new_prof.get("node"))
                            impersonate = new_prof["impersonate"]
                            profile_changed = True
                            empty_streak = 0
                            backoff.register_success()
                            auth_dead = False
                        except Exception as e:
                            log.error("[%s] не смог применить impersonate=%s (версия curl_cffi в "
                                      "контейнере старее хоста?): %s", name, new_prof["impersonate"], e)

                    cookie_watcher.min_check_interval = config.get("cookie_reload_check_interval_seconds", 30)
                    new_cookies = cookie_watcher.load_if_changed(force=profile_changed or force_profile)
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
                            # Пауза с редкими пробами; новые cookies
                            # (hot-reload выше) сами возобновляют работу.
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

                        # Долго пусто -> просим демона сменить идентичность
                        # (нода + отпечаток + cookies). Сами ничего не меняем.
                        if (rot_on and empty_streak >= int(rot.get("empty_streak_trigger", 6))
                                and time.time() >= next_request_at):
                            try:
                                identity.write_request(name, "empty_listing", empty_streak)
                                next_request_at = time.time() + float(rot.get("request_cooldown_seconds", 600))
                                cooldown = ROTATION_POLL_SECONDS
                                log.warning("[%s] пусто %d раз подряд — заявка на смену идентичности "
                                            "(state/requests), опрос на паузе", name, empty_streak)
                            except OSError as e:
                                log.error("[%s] не смог записать заявку на ротацию (state/ смонтирован "
                                          "на запись?): %s", name, e)
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
                    payloads.sort(key=lambda p: p["_age_seconds"])

                    # Комментарии не отправляются сразу: они claim'ятся и уходят в
                    # буфер Sender'а, который шлёт их батчами по состоянию очереди
                    # bpipe, самые свежие первыми.
                    to_buffer: list[dict] = []
                    dropped_dup = 0
                    for p in payloads:
                        if not await seen.try_claim(p["external_id"]):
                            dropped_dup += 1
                            continue
                        to_buffer.append(p)
                    sent = await sender.put_many(to_buffer)

                    outcome = PollOutcome(
                        page_len=stats["first_len"], dupes=stats["first_dupes"],
                        pages=result.pages_fetched, overlap_found=stats["overlap"],
                    )
                    cooldown = rl_cooldown(result.ratelimit, margin, rl_max)

                    log.info(
                        "[%s] r/%s стр=%d получено=%d дубли_стр1=%d/%d в_буфер=%d "
                        "дубли=%d буфер=%d догнали=%s cooldown=%.1fs",
                        name, subs_joined, result.pages_fetched, len(comments),
                        stats["first_dupes"], stats["first_len"], sent,
                        dropped_dup, sender.backlog, stats["overlap"], cooldown,
                    )
                finally:
                    scheduler.release(slot, outcome, cooldown, sent)
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
