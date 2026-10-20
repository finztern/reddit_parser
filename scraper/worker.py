import asyncio
import time

import aiohttp

from . import identity
from .config import ConfigStore, CookieFileWatcher
from .constants import BASE_DIR, DEFAULT_CONNECT_TIMEOUT_SECONDS, PROXY_HOST, log
from .fleet_polling import AUTH_FAILED
from .geo import build_accept_language, pick_locale
from .profile_gen import load_profile as load_device_profile, refresh_geo as refresh_profile_geo
from .health import ExecutorHandle, ExecutorHealth
from .http_client import CurlSessionHandle, fetch_comments
from .pipeline import build_payload
from .utils import is_nsfw
from .scheduler import PollOutcome, PollScheduler
from .sender import Sender
from .state import BackoffState, SeenCache
from .sub_rotation import pick_subs

# Как часто проверять появление/починку cookies-файла, пока его нет
# (гостевой слот ещё не харвестился) или он не читается.
COOKIE_WAIT_SECONDS = 60

# Кулдаун при пустом листинге (200 + пустой Listing на первой странице) считает
# AccountHealth.on_empty (fleet_polling.empty_cooldown_*). После EMPTY_SWAP_AFTER
# пустых подряд пересоздаём curl-сессию (свежее соединение).
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
    metrics=None,  # MetricsCollector | None (модуль 06); None — метрики выключены
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

    # Постоянный профиль устройства (модуль 03, state/profiles/account_N.json) главнее и
    # accounts.yaml, и слота: отпечаток аккаунта не меняется, меняется только гео (смена ноды).
    ident = load_device_profile(name)
    known_node = (prof or {}).get("node")
    if ident:
        impersonate = ident["impersonate"]
        user_agent = ident["user_agent"]
        log.info("[%s] профиль устройства: %s %s locale=%s tz=%s", name, ident["os"], impersonate,
                 ident["locale"], ident["timezone_id"])
    else:
        log.warning("[%s] нет state/profiles/%s.json — беру отпечаток из accounts.yaml "
                    "(создай: scripts/profile.py --migrate / --create)", name, name)

    # NB: реальные запросы к Reddit идут через curl_cffi с impersonate (он
    # сам ставит UA/заголовки, соответствующие TLS-отпечатку). Эти headers
    # живут только в aiohttp-сессии (хранилище cookie_jar).
    # Локаль = гео ноды слота: профиль после ротации (state/slots) ->
    # accounts.yaml:locale -> en-US. Из неё строится Accept-Language для
    # curl-запросов (формат зависит от семейства браузера по impersonate).
    locale = ident["locale"] if ident else pick_locale(prof, account)
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": build_accept_language(locale, impersonate),
    }
    curl_session = CurlSessionHandle(
        impersonate=impersonate, proxies={"http": proxy_url, "https": proxy_url},
        locale=locale,
    )
    curl_session.set_profile_headers(ident)
    backoff = BackoffState(
        base_seconds=config.get("base_backoff_seconds", 5),
        max_seconds=config.get("max_backoff_seconds", 300),
        max_auth_errors=scheduler.cfg().auth_failed_after,  # модуль 05: fleet_polling.auth_failed_after
    )
    auth_dead = False  # cookies отвергнуты — ждём обновления файла
    empty_streak = 0   # подряд пустых листингов (200, children=[]) на 1-й странице
    sub_poll_no = 0    # номер опроса аккаунта — сдвигает окно сабов (stream_subs_per_request)
    next_request_at = 0.0  # не просить ротацию раньше (wall clock)

    slot = scheduler.register(name)
    health = slot.health  # состояние аккаунта (модуль 05): active/cooldown/degraded/auth_failed/...
    log.info("[%s] Старт (прокси %s, impersonate=%s, Accept-Language=%s) — жду слоты планировщика",
             name, proxy_url, impersonate, curl_session.accept_language)

    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
            while True:
                await slot.queue.get()  # слот выдан планировщиком

                outcome: PollOutcome | None = None
                result = None
                cooldown = 0.0
                sent = 0
                kind: str | None = None  # итог цикла для счётчиков планировщика
                try:
                    fp = scheduler.cfg()          # fleet_polling (читается на лету)
                    now_m = scheduler.now()
                    # ---- ротация идентичности (демон на хосте) ----
                    rot = config.get("rotation", {}) or {}
                    fleet = config.get("fleet", {}) or {}
                    # логин-слот (login: true) при node_replace_only: сгоревшая нода -> заявка демону на
                    # ЗАМЕНУ НОДЫ (профиль и сессия остаются). Гостевая ротация — только для rotation.slots.
                    login_replace = bool(account.get("login")) and bool(fleet.get("node_replace_only", True))
                    rot_on = bool(rot.get("enabled", False)) and (
                        name in (rot.get("slots") or []) or login_replace)
                    force_profile = False
                    if rot_on:
                        req = identity.read_request(name)
                        if req:
                            st = req.get("status")
                            age = time.time() - float(req.get("ts") or 0)
                            if st in ("pending", "working") and age < float(rot.get("request_timeout_seconds", 1800)):
                                cooldown = ROTATION_POLL_SECONDS
                                kind = "skipped"
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
                                    health.reset(now_m, "rotation_done")
                                    if metrics is not None:
                                        metrics.event(name, "node_replaced", "rotation_done")
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
                    drop_nsfw = bool(config.get("drop_nsfw", True))
                    max_pages = config.get("pagination_max_pages", 5)
                    pg_min = config.get("pagination_delay_min_seconds", 0.1)
                    pg_max = config.get("pagination_delay_max_seconds", 0.4)
                    respect_rl = bool(config.get("respect_ratelimit_headers", True))
                    rl_max = config.get("ratelimit_max_interval_seconds", 900)
                    timeout = config.get("request_timeout_seconds", 10)
                    connect_timeout = config.get("connect_timeout_seconds", DEFAULT_CONNECT_TIMEOUT_SECONDS)
                    base_url = config.get("reddit_base_url", "https://www.reddit.com")
                    fetch_limit = min(100, config.get("fetch_limit", 100))
                    subs_joined = "+".join(pick_subs(
                        config.get("stream_subs", ["all"]), config.get("stream_subs_per_request", 0),
                        name, sub_poll_no, config.get("stream_subs_all_chance", 1.0)))
                    sub_poll_no += 1

                    # Профиль (impersonate) — раньше cookies: демон пишет
                    # state/slots/* ДО замены cookies-файла.
                    new_prof = profile_watcher.load_if_changed(force=force_profile)
                    profile_changed = False
                    if ident is not None and new_prof is not None:
                        # Профиль устройства постоянен: слот-профиль лишь сообщает о смене ноды ->
                        # перепроверяем ГЕО (locale/timezone/язык), отпечаток не трогаем.
                        if new_prof.get("node") != known_node:
                            known_node = new_prof.get("node")
                            try:
                                upd = await asyncio.to_thread(refresh_profile_geo, name, proxy_url)
                            except Exception as e:
                                log.warning("[%s] гео после смены ноды не определено: %s", name, e)
                                upd = None
                            if upd is not None:
                                ident = upd
                                if ident["locale"] != locale:
                                    log.info("[%s] локаль %s -> %s (новый выходной IP)", name, locale, ident["locale"])
                                    locale = ident["locale"]
                                    curl_session.set_locale(locale)
                        new_prof = None
                    if new_prof is not None:
                        # гео ноды могло смениться вместе с профилем
                        new_locale = pick_locale(new_prof, account)
                        if new_locale != locale:
                            log.info("[%s] локаль %s -> %s", name, locale, new_locale)
                            locale = new_locale
                            curl_session.set_locale(locale)
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
                            health.reset(now_m, "profile_changed")
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
                        if health.state == AUTH_FAILED:
                            health.reset(now_m, "new_cookies")
                            if metrics is not None:
                                metrics.event(name, "relogin_ok", "новые cookies применены")
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
                        # аварийный тормоз: экспоненциальный cooldown аккаунта, остальные
                        # подхватывают его долю (слоты парка глобальные)
                        kind = "rate_or_server"
                        cooldown = health.on_rate_limited(now_m, fp, result.retry_after)
                        log.warning("[%s] %s — аккаунт в cooldown %.1fs (подряд: %d)",
                                    name, result.status, cooldown, health.rate_streak)
                        continue

                    if result.error_kind == "auth":
                        kind = "auth"
                        cooldown, _ = backoff.register_auth_error()
                        # auth_failed после fleet_polling.auth_failed_after (по умолчанию 3) подряд 401/403
                        should_stop = health.on_auth_error(now_m, fp, result.status) == AUTH_FAILED
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
                            # логин-слот и 403 подряд: нода сгорела (401 — это сессия, ноде не поможет)
                            if (login_replace and rot_on and result.status == 403
                                    and time.time() >= next_request_at):
                                try:
                                    identity.write_request(name, "blocked_403", backoff.consecutive_auth_errors)
                                    health.mark_blocked_node(now_m)  # до замены ноды не опрашиваем
                                    next_request_at = time.time() + float(rot.get("request_cooldown_seconds", 600))
                                    cooldown = ROTATION_POLL_SECONDS
                                    log.warning("[%s] 403 подряд %d раз — заявка демону на замену ноды "
                                                "(state/requests), опрос на паузе",
                                                name, backoff.consecutive_auth_errors)
                                except OSError as e:
                                    log.error("[%s] не смог записать заявку на замену ноды: %s", name, e)
                        else:
                            log.warning("[%s] %s — отдых %.1fs (auth-ошибок %d/%d)", name, result.status,
                                        cooldown, health.auth_streak, fp.auth_failed_after)
                        continue

                    if result.error_kind == "empty":
                        # 200 + пустой Listing на первой странице: мягкий
                        # троттлинг/пустой ответ edge. Это НЕ успех — аккаунт
                        # отдыхает с нарастающим кулдауном, чтобы не жечь слоты.
                        kind = "empty"
                        empty_streak += 1
                        cooldown = health.on_empty(now_m, fp)  # degraded после degraded_after_empty подряд
                        # если Reddit прислал аномальные X-Ratelimit-* — уважаем и их (аварийный тормоз)
                        if respect_rl:
                            cooldown = max(cooldown, health.on_emergency_ratelimit(
                                now_m, fp, result.ratelimit, rl_max))
                        if empty_streak >= EMPTY_SWAP_AFTER:
                            curl_session.swap()  # свежее соединение/keep-alive
                        log.warning("[%s] пустой листинг подряд %d — отдых %.1fs (состояние: %s)",
                                    name, empty_streak, cooldown, health.state)

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
                        kind = "network"
                        health.on_network_error(now_m)
                        cooldown = min(backoff.register_rate_or_server_error(), 30.0)
                        continue

                    # ---- успех ----
                    kind = "ok"
                    backoff.register_success()
                    health.on_ok(now_m, fp)
                    auth_dead = False
                    empty_streak = 0
                    comments = result.comments

                    payloads = []
                    nsfw_ids: list[str] = []
                    for c in comments:
                        p = build_payload(c)
                        if p is None or p["_age_seconds"] > max_age:
                            continue
                        if drop_nsfw and is_nsfw(c):
                            nsfw_ids.append(p["external_id"])
                            continue
                        payloads.append(p)
                    # NSFW не отправляем, но помечаем увиденным: иначе остановка пагинации
                    # по пересечению (stop_when) не увидит знакомых id и листала бы лишнее.
                    for nid in nsfw_ids:
                        if await seen.try_claim(nid):
                            await seen.confirm(nid)
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
                    # Лимиты — только аварийный тормоз: обычные X-Ratelimit-* темп не меняют
                    # (темп парка диктует поток), реагируем лишь на почти исчерпанный остаток.
                    cooldown = (health.on_emergency_ratelimit(now_m, fp, result.ratelimit, rl_max)
                                if respect_rl else 0.0)

                    log.info(
                        "[%s] r/%s стр=%d получено=%d дубли_стр1=%d/%d в_буфер=%d "
                        "дубли=%d буфер=%d догнали=%s cooldown=%.1fs",
                        name, subs_joined, result.pages_fetched, len(comments),
                        stats["first_dupes"], stats["first_len"], sent,
                        dropped_dup, sender.backlog, stats["overlap"], cooldown,
                    )
                finally:
                    if metrics is not None and kind and kind != "skipped":
                        # O(1), без ввода-вывода: запись метрик цикл опроса не блокирует
                        metrics.record_poll(
                            name, kind, status=getattr(result, "status", None),
                            requests=(result.pages_fetched if kind == "ok" and result is not None else 1),
                            fetched=(len(result.comments) if kind == "ok" and result is not None else 0),
                            unique=sent, state=health.effective_state(scheduler.now()),
                        )
                    scheduler.release(slot, outcome, cooldown, sent, kind)
    finally:
        curl_session.close()
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