import asyncio
import functools
import json
import logging
import random
import threading
import time
from curl_cffi import requests as cffi_requests

import aiohttp

from .constants import DEFAULT_CONNECT_TIMEOUT_SECONDS, HTTP_EXECUTOR_SLACK_SECONDS, log
from .geo import build_accept_language
from .health import ExecutorHandle, ExecutorHealth
from .models import FetchResult

# Потолок страниц, когда pagination_max_pages <= 0 ("без лимита"):
# листинг Reddit всё равно не отдаёт глубже ~1000 элементов (10 x 100),
# а пагинация и так останавливается по max_age / пересечению с виденными.
UNLIMITED_PAGES_CAP = 10

# Заголовки, которые логируем при пустом листинге (диагностика: edge-кэш,
# CDN, троттлинг).
_DEBUG_HEADERS = (
    "x-cache", "x-served-by", "age", "cf-ray", "via", "cache-control",
    "x-ratelimit-remaining", "x-ratelimit-reset", "x-ratelimit-used",
)


def parse_retry_after(resp) -> float | None:
    """Reddit может прислать Retry-After в секундах (числом)."""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_ratelimit_headers(resp) -> dict | None:
    """Разбирает X-Ratelimit-Remaining / Reset (и опционально Used).
    Отбраковывает аномальные значения (reset вне [0, 3600], remaining < 0)."""
    try:
        remaining = resp.headers.get("X-Ratelimit-Remaining") or resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("X-Ratelimit-Reset") or resp.headers.get("x-ratelimit-reset")
        used = resp.headers.get("X-Ratelimit-Used") or resp.headers.get("x-ratelimit-used")
        if remaining is None or reset is None:
            return None
        remaining_f = float(remaining)
        reset_f = float(reset)
        if remaining_f < 0 or not (0 <= reset_f <= 3600):
            log.warning(
                "Reddit прислал аномальные X-Ratelimit-заголовки "
                "(remaining=%s reset=%s) — игнорирую, как будто их не было",
                remaining, reset,
            )
            return None
        return {
            "remaining": remaining_f,
            "reset": reset_f,
            "used": float(used) if used is not None else None,
        }
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- #
#  Переиспользуемая curl_cffi.Session на аккаунт (keep-alive)
# ---------------------------------------------------------------- #

class CurlSessionHandle:
    """Session на аккаунт. При таймауте не закрывается (зависший поток
    может ещё держать её), а заменяется новой через swap(). swap() также
    вызывается при hot-reload cookies: сессия копит Set-Cookie от Reddit,
    и устаревшие значения не должны конфликтовать со свежими из файла."""

    def __init__(self, impersonate: str, proxies: dict | None, locale: str | None = None):
        self._impersonate = impersonate
        self._proxies = proxies
        self._locale = locale
        self.accept_language = build_accept_language(locale, impersonate)
        # Заголовки из постоянного профиля (модуль 03): User-Agent, sec-ch-ua*.
        # Accept-Language добавляется отдельно (зависит от locale).
        self.extra_headers: dict[str, str] = {}
        self.current = self._build()
        self.swaps = 0
        self._lock = threading.Lock()
        self._in_flight: dict[int, int] = {}
        self._retired: dict[int, cffi_requests.Session] = {}
        self._closed = False

    def set_profile_headers(self, profile: dict | None):
        """UA и sec-ch-ua* из профиля устройства — те же значения видит браузер."""
        self.extra_headers = {}
        if not profile:
            return
        from .profile_gen import http_headers
        self.extra_headers = {k: v for k, v in http_headers(profile).items() if k != "Accept-Language"}

    def request_headers(self) -> dict[str, str]:
        return {**self.extra_headers, "Accept-Language": self.accept_language}

    def set_locale(self, locale: str | None):
        """Смена локали (гео ноды) на лету; сессию пересоздавать не нужно —
        заголовок подставляется на каждый запрос."""
        self._locale = locale
        self.accept_language = build_accept_language(locale, self._impersonate)

    def _build(self) -> cffi_requests.Session:
        return cffi_requests.Session(
            impersonate=self._impersonate,
            proxies=self._proxies,
            use_thread_local_curl=False,
        )

    def acquire(self):
        """Pin the current session until its executor-thread request finishes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("curl session handle is closed")
            session = self.current
            key = id(session)
            self._in_flight[key] = self._in_flight.get(key, 0) + 1
            return session

    def release(self, session) -> None:
        close_session = None
        key = id(session)
        with self._lock:
            remaining = self._in_flight.get(key, 0) - 1
            if remaining > 0:
                self._in_flight[key] = remaining
            else:
                self._in_flight.pop(key, None)
                close_session = self._retired.pop(key, None)
        self._close(close_session)

    @staticmethod
    def _close(session) -> None:
        if session is not None:
            try:
                close = getattr(session, "close", None)
                if close is not None:
                    close()
            except Exception:
                log.debug("Не удалось закрыть заменённую curl-сессию", exc_info=True)

    def _replace(self, new_session) -> None:
        close_session = None
        rejected = False
        with self._lock:
            if self._closed:
                rejected = True
            else:
                old_session = self.current
                self.current = new_session
                key = id(old_session)
                if self._in_flight.get(key, 0):
                    self._retired[key] = old_session
                else:
                    close_session = old_session
                self.swaps += 1
        if rejected:
            self._close(new_session)
            raise RuntimeError("curl session handle is closed")
        self._close(close_session)

    def close(self) -> None:
        close_session = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            session = self.current
            key = id(session)
            if self._in_flight.get(key, 0):
                self._retired[key] = session
            else:
                close_session = session
        self._close(close_session)

    def swap(self):
        self._replace(self._build())

    def set_impersonate(self, impersonate: str):
        """Смена TLS-отпечатка на лету (ротация идентичности). Новая
        сессия строится ДО подмены: если таргета нет в установленном
        curl_cffi, исключение вылетает, а старая сессия остаётся рабочей."""
        new = cffi_requests.Session(
            impersonate=impersonate,
            proxies=self._proxies,
            use_thread_local_curl=False,
        )
        self._impersonate = impersonate
        # формат Accept-Language зависит от семейства браузера
        self.accept_language = build_accept_language(self._locale, impersonate)
        self._replace(new)


async def _fetch_comments_page(
    session: aiohttp.ClientSession,
    base_url: str,
    subs_joined: str,
    fetch_limit: int,
    curl_session: CurlSessionHandle,
    timeout: int,
    account_name: str,
    http_executor: ExecutorHandle,
    after: str | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    executor_health: ExecutorHealth | None = None,
) -> FetchResult:
    """Один HTTP-запрос к .../comments.json (одна страница листинга).
    http_executor.current и curl_session.current резолвятся здесь, на
    каждый запрос."""
    url = f"{base_url}/r/{subs_joined}/comments.json"
    params = {"limit": fetch_limit}
    if after:
        params["after"] = after

    cookies = {c.key: c.value for c in session.cookie_jar}

    curl = curl_session.acquire()
    call = functools.partial(
        curl.get,
        url,
        params=params,
        cookies=cookies,
        headers=curl_session.request_headers(),
        timeout=(connect_timeout, timeout),
        allow_redirects=True,
    )

    total_wait = connect_timeout + timeout + HTTP_EXECUTOR_SLACK_SECONDS

    resp = None
    try:
        loop = asyncio.get_running_loop()
        try:
            request_state = {"started": False, "cancelled": False, "released": False}
            request_lock = threading.Lock()

            def release_once():
                with request_lock:
                    if request_state["released"]:
                        return
                    request_state["released"] = True
                curl_session.release(curl)

            def run_call():
                with request_lock:
                    if request_state["cancelled"]:
                        return None
                    request_state["started"] = True
                try:
                    return call()
                finally:
                    release_once()

            future = loop.run_in_executor(http_executor.current, run_call)
        except BaseException:
            curl_session.release(curl)
            raise

        def release_if_cancelled_before_start(done):
            if not done.cancelled():
                return
            with request_lock:
                if request_state["started"]:
                    return
                request_state["cancelled"] = True
            release_once()

        future.add_done_callback(release_if_cancelled_before_start)

        if executor_health is not None:
            generation = executor_health.on_submit()
            future.add_done_callback(
                lambda _f, g=generation: executor_health.on_future_done(g)
            )

        resp = await asyncio.wait_for(future, timeout=total_wait)
        ratelimit = parse_ratelimit_headers(resp)

        if resp.status_code == 429:
            retry_after = parse_retry_after(resp)
            return FetchResult(status=429, retry_after=retry_after, error_kind="rate_or_server",
                                ratelimit=ratelimit)
        if resp.status_code in (401, 403):
            return FetchResult(status=resp.status_code, error_kind="auth", ratelimit=ratelimit)
        if resp.status_code >= 500:
            return FetchResult(status=resp.status_code, error_kind="rate_or_server", ratelimit=ratelimit)
        if resp.status_code != 200:
            log.warning("[%s] Reddit вернул неожиданный статус %s", account_name, resp.status_code)
            return FetchResult(status=resp.status_code, error_kind="rate_or_server", ratelimit=ratelimit)

        data = resp.json()

    except asyncio.TimeoutError:
        if executor_health is not None:
            executor_health.on_wait_timeout()
        log.warning(
            "[%s] Запрос к Reddit не уложился в %.0fs (connect=%.0fs+read=%.0fs+запас) — "
            "проверь mihomo/порт; пересоздаю curl-сессию аккаунта",
            account_name, total_wait, connect_timeout, timeout,
        )
        curl_session.swap()
        return FetchResult(error_kind="network")
    except cffi_requests.RequestsError as e:
        log.warning("[%s] Ошибка запроса к Reddit (проверь mihomo/порт): %s", account_name, e)
        return FetchResult(error_kind="network")
    except (json.JSONDecodeError, ValueError) as e:
        body = ""
        try:
            body = resp.text if resp is not None else ""
        except Exception:
            pass
        try:
            hdrs = {k: v for k, v in resp.headers.items() if k.lower() in _DEBUG_HEADERS + ("content-type",)}
        except Exception:
            hdrs = {}
        log.warning(
            "[%s] 200, но не JSON: len=%d body=%r hdr=%s (%s)",
            account_name, len(body), body[:200], hdrs, e,
        )
        # пустое тело или HTML-заглушка = мягкий бан, а не сетевая ошибка:
        # пусть сработает путь "empty" (рост кулдауна + заявка на ротацию)
        if not body.strip() or body.lstrip()[:1] == "<":
            return FetchResult(status=200, error_kind="empty", ratelimit=parse_ratelimit_headers(resp) if resp is not None else None)
        return FetchResult(error_kind="network")

    listing_data = data.get("data", {}) if isinstance(data, dict) else {}
    children = listing_data.get("children", [])

    if not children:
        # Пустая ПЕРВАЯ страница (after не задан) — аномалия: мягкий
        # троттлинг / пустой ответ edge-кэша. Пустая страница при
        # пагинации (after задан) — обычный конец листинга.
        first_page = not after
        try:
            hdrs = {k: v for k, v in resp.headers.items() if k.lower() in _DEBUG_HEADERS}
        except Exception:
            hdrs = {}
        log.log(
            logging.WARNING if first_page else logging.DEBUG,
            "[%s] пустой листинг (%s): status=%s dist=%s rl=%s hdr=%s",
            account_name, "стр.1" if first_page else "стр.N",
            resp.status_code, listing_data.get("dist"), ratelimit, hdrs,
        )
        if first_page:
            return FetchResult(status=200, error_kind="empty", ratelimit=ratelimit)

    comments = [c.get("data", {}) for c in children if c.get("kind") == "t1"]

    listing_after = listing_data.get("after")
    if not listing_after and comments:
        listing_after = comments[-1].get("name")

    return FetchResult(comments=comments, status=200, after=listing_after, ratelimit=ratelimit)


async def fetch_comments(
    session: aiohttp.ClientSession,
    base_url: str,
    subs_joined: str,
    fetch_limit: int,
    curl_session: CurlSessionHandle,
    timeout: int,
    account_name: str,
    http_executor: ExecutorHandle,
    max_age: float,
    max_pages: int,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    pagination_delay_min: float = 0.0,
    pagination_delay_max: float = 0.0,
    executor_health: ExecutorHealth | None = None,
    stop_when=None,
) -> FetchResult:
    """Тянет одну или несколько страниц .../comments.json подряд.

    `stop_when(page_comments) -> bool` (опционально) вызывается после
    каждой успешной непустой страницы; True — пагинация останавливается
    ("дошли до уже виденных комментариев").

    max_pages <= 0 означает "без лимита" (на деле — не более
    UNLIMITED_PAGES_CAP; раньше 0 приводил к остановке сразу после
    первой страницы).

    Ошибка на любой странице обрывает пагинацию и возвращается целиком —
    накопленные комментарии отбрасываются (безопасно: они ещё не попали
    в SeenCache и будут подхвачены на следующем опросе). Пустая первая
    страница возвращается как error_kind="empty"."""
    page_cap = max_pages if max_pages and max_pages > 0 else UNLIMITED_PAGES_CAP
    all_comments: list[dict] = []
    after: str | None = None
    pages_fetched = 0

    while True:
        page = await _fetch_comments_page(
            session, base_url, subs_joined, fetch_limit, curl_session, timeout,
            account_name, http_executor, after, connect_timeout,
            executor_health,
        )

        if page.error_kind is not None:
            return page

        pages_fetched += 1
        all_comments.extend(page.comments)

        if not page.comments:
            break

        if stop_when is not None and stop_when(page.comments):
            break

        last_created = page.comments[-1].get("created_utc")
        last_age = (time.time() - last_created) if last_created is not None else None

        if last_age is None or last_age > max_age:
            break
        if not page.after:
            break
        if pages_fetched >= page_cap:
            log.info(
                "[%s] достигнут лимит страниц (%d), пересечения с виденными так и нет "
                "(age последнего=%.1fs <= max_age=%ss) — вероятно, пропуск комментариев",
                account_name, page_cap, last_age, max_age,
            )
            break

        if pagination_delay_max > 0:
            lo = min(pagination_delay_min, pagination_delay_max)
            hi = max(pagination_delay_min, pagination_delay_max)
            await asyncio.sleep(random.uniform(lo, hi))

        after = page.after

    return FetchResult(comments=all_comments, status=200, pages_fetched=pages_fetched,
                        ratelimit=page.ratelimit)
