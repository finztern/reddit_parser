"""
scripts/human_session.py — модуль 04: «живое» поведение браузера при обслуживании сессии.

ТОЛЬКО ЧТЕНИЕ. Открываем ленту, плавно скроллим, иногда возвращаемся вверх, открываем 1–2 поста,
держим их и возвращаемся. Никаких лайков/апвоутов/подписок/комментариев/сообщений/сохранений —
и это обеспечено не только кодом, но и сетевым предохранителем (WriteGuard): любой не-GET запрос
на запись блокируется на уровне контекста Playwright.

Используется из scripts/refresh_cookies.py (плановое обновление) и scripts/login_hub.py (после
первого входа). Скрапер на curl_cffi этот модуль не трогает.

Конфиг — секция `human:` в config.yaml (см. DEFAULTS ниже).

Статусы результата:
  ok               — сессия отработала в заданном окне
  skipped          — выключено / ещё не время / обновление не нужно
  needs_attention  — капча, разлогин, пустая лента, ошибка навигации (аккаунт надо посмотреть руками)
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import yaml

OK, SKIPPED, NEEDS_ATTENTION = "ok", "skipped", "needs_attention"

DEFAULTS: dict = {
    "enabled": True,
    "profile": "normal",            # calm | normal
    "min_interval_hours": 24,       # не чаще раза в N часов на аккаунт
    "interval_jitter": 0.25,        # следующий срок = interval * (1 + U(0, jitter)) — аккаунты расходятся
    "refresh_before_hours": 12,     # сессия «нуждается в обновлении», если токен истекает раньше
    "min_session_seconds": 40,
    "max_session_seconds": 150,     # жёсткий потолок длительности сессии
    "on_first_login": True,         # короткая сессия после первого входа через login_hub
    "start_gap_seconds": 45,        # средний разброс старта между аккаунтами (log-normal)
    # POST на эти хосты допускается (чтение через GraphQL), если в теле нет маркеров записи
    "allow_post_hosts": ["gql.reddit.com", "gql-fed.reddit.com"],
    "write_markers": ["mutation", "vote", "subscribe", "follow", "comment", "submit", "save", "report",
                      "block", "message", "award", "crosspost", "delete", "edit"],
}

# pause — паузы между действиями (с), строго в рамках 0.8–6 с из ТЗ
PROFILES: dict[str, dict] = {
    "calm": {"pause": (1.4, 6.0), "chunk_px": (140, 360), "posts": (1, 1), "up_prob": 0.15, "mouse_moves": (1, 2)},
    "normal": {"pause": (0.8, 4.5), "chunk_px": (220, 520), "posts": (1, 2), "up_prob": 0.25, "mouse_moves": (1, 3)},
}
PAUSE_MIN, PAUSE_MAX = 0.8, 6.0

CAPTCHA_MARKERS = ("captcha", "verify you are human", "are you a human", "prove you're not a robot",
                   "whoa there, pardner", "blocked by network security")
POST_HREF_RE = re.compile(r"^(?:https?://[^/]*reddit\.com)?/r/[^/]+/comments/", re.I)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------- #
#  Конфиг
# ---------------------------------------------------------------- #

def load_human_cfg(config_path: Path | None = None) -> dict:
    if config_path is None:
        from scraper.constants import CONFIG_PATH
        config_path = CONFIG_PATH
    try:
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    cfg = {**DEFAULTS, **(data.get("human") or {})}
    if cfg["profile"] not in PROFILES:
        cfg["profile"] = "normal"
    cfg["min_session_seconds"] = float(cfg["min_session_seconds"])
    cfg["max_session_seconds"] = max(cfg["min_session_seconds"], float(cfg["max_session_seconds"]))
    return cfg


# ---------------------------------------------------------------- #
#  Случайность: лог-нормальные задержки
# ---------------------------------------------------------------- #

def lognormal_between(rng: random.Random, lo: float, hi: float) -> float:
    """Лог-нормальное значение с медианой на геометрическом среднем [lo, hi], обрезанное по границам.
    Короткие паузы чаще, длинные — редким «хвостом» (так читает человек), а не равномерно."""
    if hi <= lo:
        return lo
    mu = math.log(math.sqrt(lo * hi))
    sigma = math.log(hi / lo) / 3.0
    return min(hi, max(lo, rng.lognormvariate(mu, sigma)))


def pick_pause(rng: random.Random, prof: dict) -> float:
    lo, hi = prof["pause"]
    return lognormal_between(rng, max(PAUSE_MIN, lo), min(PAUSE_MAX, hi))


def plan_start_offsets(names: list[str], rng: random.Random, mean_gap: float = 45.0,
                       min_gap: float | None = None) -> dict[str, float]:
    """Смещения старта аккаунтов (с от начала прогона): разные для всех, зазоры лог-нормальные,
    порядок перемешан — не «по номеру» и не все сразу."""
    min_gap = mean_gap * 0.35 if min_gap is None else min_gap
    order = list(names)
    rng.shuffle(order)
    t, out = 0.0, {}
    for i, n in enumerate(order):
        if i:
            t += max(min_gap, lognormal_between(rng, mean_gap * 0.4, mean_gap * 2.2))
        out[n] = round(t, 3)
    return out


# ---------------------------------------------------------------- #
#  Состояние: «не чаще раза в N часов» и нужно ли обновление
# ---------------------------------------------------------------- #

def _human_state_dir(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from scraper.identity import STATE_DIR
    return STATE_DIR / "human"


def read_state(name: str, state_dir: Path | None = None) -> dict:
    p = _human_state_dir(state_dir) / f"{name}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(name: str, data: dict, state_dir: Path | None = None) -> None:
    d = _human_state_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / f"{name}.json")


def is_due(state: dict, now: float) -> bool:
    return now >= float(state.get("next_due_ts") or 0)


def next_due_ts(now: float, cfg: dict, rng: random.Random) -> float:
    base = float(cfg["min_interval_hours"]) * 3600
    return now + base * (1 + rng.uniform(0, float(cfg["interval_jitter"])))


def needs_refresh(storage_state_path: Path | None, cookies: list[dict], cfg: dict, now: float) -> tuple[bool, str]:
    """Сессия «действительно нуждается в обновлении»: нет сохранённого storage_state, он старше
    min_interval_hours, либо ключевая cookie скоро истекает. Реже = безопаснее."""
    if storage_state_path is None or not Path(storage_state_path).exists():
        return True, "нет сохранённого storage_state"
    age_h = (now - Path(storage_state_path).stat().st_mtime) / 3600
    if age_h >= float(cfg["min_interval_hours"]):
        return True, f"storage_state старше {age_h:.0f} ч"
    horizon = now + float(cfg["refresh_before_hours"]) * 3600
    for c in cookies or []:
        if c.get("name") in ("token_v2", "reddit_session"):
            exp = c.get("expires", c.get("expirationDate", -1))
            try:
                exp = float(exp)
            except (TypeError, ValueError):
                continue
            if 0 < exp < horizon:
                return True, f"{c['name']} истекает в ближайшие {cfg['refresh_before_hours']} ч"
    return False, "сессия свежая"


def should_run(name: str, cfg: dict, storage_state_path: Path | None, cookies: list[dict], *,
               now: float | None = None, force: bool = False, state_dir: Path | None = None) -> tuple[bool, str]:
    now = time.time() if now is None else now
    if not cfg.get("enabled"):
        return False, "human.enabled=false"
    if force:
        return True, "force"
    if not is_due(read_state(name, state_dir), now):
        return False, "ещё не прошёл min_interval_hours"
    need, why = needs_refresh(storage_state_path, cookies, cfg, now)
    return (True, why) if need else (False, why)


# ---------------------------------------------------------------- #
#  Предохранитель: ни одного запроса на запись
# ---------------------------------------------------------------- #

class WriteGuard:
    """Вешается на context.route('**/*'). Блокирует POST/PUT/PATCH/DELETE. Исключение — POST на
    GraphQL-хосты (там Reddit читает ленту), и только если в теле нет маркеров записи."""

    def __init__(self, allow_hosts=(), write_markers=()):
        self.allow_hosts = tuple(h.lower() for h in allow_hosts)
        self.markers = tuple(m.lower() for m in write_markers)
        self.seen: list[tuple[str, str]] = []       # все (метод, url)
        self.blocked: list[tuple[str, str]] = []    # заблокированные записи
        self.passed_unsafe: list[tuple[str, str]] = []  # прошедшие POST на чтение (gql)

    def decide(self, method: str, url: str, post_data: str | None = None) -> bool:
        method = (method or "GET").upper()
        self.seen.append((method, url))
        if method not in UNSAFE_METHODS:
            return True
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        body = (post_data or "").lower()
        if method == "POST" and host in self.allow_hosts and not any(m in body for m in self.markers):
            self.passed_unsafe.append((method, url))
            return True
        self.blocked.append((method, url))
        return False

    def _handler(self, route, request):
        try:
            data = request.post_data
        except Exception:  # бинарное тело
            data = None
        if self.decide(request.method, request.url, data):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def install(self, context) -> None:
        context.route("**/*", self._handler)


# ---------------------------------------------------------------- #
#  Движения мыши и скролл
# ---------------------------------------------------------------- #

def mouse_path(rng: random.Random, start: tuple[float, float], bounds: tuple[float, float]) -> list[tuple[float, float]]:
    """Небольшая случайная траектория (квадратичная кривая Безье с шумом) внутри окна."""
    w, h = bounds
    sx, sy = start
    ex = min(w - 5, max(5, sx + rng.uniform(-160, 160)))
    ey = min(h - 5, max(5, sy + rng.uniform(-110, 110)))
    cx = (sx + ex) / 2 + rng.uniform(-40, 40)
    cy = (sy + ey) / 2 + rng.uniform(-40, 40)
    n = rng.randint(8, 18)
    pts = []
    for i in range(1, n + 1):
        t = i / n
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex + rng.uniform(-1.2, 1.2)
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey + rng.uniform(-1.2, 1.2)
        pts.append((round(x, 1), round(y, 1)))
    return pts


@dataclass
class HumanResult:
    status: str
    detail: str = ""
    duration: float = 0.0
    actions: list[str] = field(default_factory=list)
    blocked_writes: int = 0

    @property
    def ok(self) -> bool:
        return self.status == OK


class _Session:
    """Один прогон. clock/sleep инъектируются — тесты идут без браузера и без реального ожидания."""

    def __init__(self, page, cfg: dict, base_url: str, rng: random.Random,
                 clock: Callable[[], float], sleep: Callable[[float], None]):
        self.page, self.cfg, self.base, self.rng = page, cfg, base_url.rstrip("/") + "/", rng
        self.clock, self._sleep = clock, sleep
        self.prof = PROFILES[cfg["profile"]]
        self.t0 = clock()
        lo, hi = cfg["min_session_seconds"], cfg["max_session_seconds"]
        # цель — с разбросом внутри окна, но заведомо с запасом до жёсткого потолка
        self.target = rng.uniform(lo, hi - 8) if hi - 8 > lo else lo
        self.hard_end = self.t0 + hi
        self.mouse = (rng.uniform(200, 600), rng.uniform(150, 450))
        self.actions: list[str] = []

    # --- время ---
    def elapsed(self) -> float:
        return self.clock() - self.t0

    def left(self) -> float:
        return self.t0 + self.target - self.clock()

    def hard_left(self) -> float:
        return self.hard_end - self.clock()

    def sleep(self, seconds: float) -> None:
        self._sleep(max(0.0, min(seconds, self.hard_left())))

    def pause(self) -> None:
        self.sleep(pick_pause(self.rng, self.prof))

    # --- действия ---
    def goto(self, url: str) -> None:
        timeout_ms = int(max(3.0, min(20.0, self.hard_left() - 2)) * 1000)
        self.page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

    def wiggle(self) -> None:
        try:
            vp = self.page.viewport_size or {"width": 1280, "height": 720}
        except Exception:
            vp = {"width": 1280, "height": 720}
        for x, y in mouse_path(self.rng, self.mouse, (vp["width"], vp["height"])):
            self.page.mouse.move(x, y)
            self.sleep(self.rng.uniform(0.008, 0.03))
            self.mouse = (x, y)
        self.actions.append("mouse")

    def scroll(self, direction: int = 1, fraction: float = 1.0) -> None:
        """Плавный скролл: кусок разбит на мелкие колёсные шаги с переменной скоростью."""
        lo, hi = self.prof["chunk_px"]
        total = self.rng.uniform(lo, hi) * fraction * (1.7 if direction < 0 else 1.0)
        done = 0.0
        while done < total and self.hard_left() > 1:
            step = min(total - done, self.rng.uniform(25, 90))
            self.page.mouse.wheel(0, direction * step)
            done += step
            self.sleep(self.rng.uniform(0.02, 0.09))
        self.actions.append("scroll_up" if direction < 0 else "scroll")

    def health(self) -> str | None:
        """None — всё в порядке, иначе причина для needs_attention."""
        url = (self.page.url or "").lower()
        if "/login" in url or "/account/" in url:
            return "сессия сбросилась (редирект на логин)"
        try:
            body = self.page.inner_text("body", timeout=3000)[:4000].lower()
        except Exception:
            body = ""
        if any(m in body for m in CAPTCHA_MARKERS):
            return "капча / блокировка"
        return None

    def post_links(self) -> list[str]:
        try:
            hrefs = self.page.eval_on_selector_all(
                "a[href*='/comments/']", "els => els.map(e => e.getAttribute('href'))") or []
        except Exception:
            return []
        seen, out = set(), []
        for h in hrefs:
            if h and POST_HREF_RE.match(h) and h not in seen:
                seen.add(h)
                out.append(urljoin(self.base, h))
        return out

    def read_post(self, url: str, feed_url: str) -> None:
        self.goto(url)
        self.actions.append("open_post")
        self.pause()
        for _ in range(self.rng.randint(1, 3)):
            if self.left() < 6:
                break
            self.scroll(1, 0.7)
            self.pause()
            if self.rng.random() < 0.5:
                self.wiggle()
        try:
            self.page.go_back(timeout=int(max(3.0, min(15.0, self.hard_left() - 2)) * 1000),
                              wait_until="domcontentloaded")
        except Exception:
            self.goto(feed_url)
        self.actions.append("back")
        self.pause()

    # --- сценарий ---
    def run(self) -> HumanResult:
        feed = self.base + self.rng.choice(["", "r/popular/"])
        self.goto(feed)
        self.actions.append("open_feed")
        self.pause()
        bad = self.health()
        if bad:
            return HumanResult(NEEDS_ATTENTION, bad, self.elapsed(), self.actions)
        links = self.post_links()
        if not links:
            self.scroll()  # лента могла быть ленивой — один шанс
            links = self.post_links()
            if not links:
                return HumanResult(NEEDS_ATTENTION, "пустая лента", self.elapsed(), self.actions)

        lo_p, hi_p = self.prof["posts"]
        n_posts = min(self.rng.randint(lo_p, hi_p), len(links))
        # моменты чтения постов — доли целевого времени, не в самом начале и не впритык к концу
        marks = sorted(self.rng.uniform(0.3, 0.75) for _ in range(n_posts))
        chosen = self.rng.sample(links, n_posts)
        while self.left() > 0 and self.hard_left() > 3:
            if marks and self.elapsed() / self.target >= marks[0] and self.left() > 14:
                marks.pop(0)
                self.read_post(chosen.pop(0), feed)
                bad = self.health()
                if bad:
                    return HumanResult(NEEDS_ATTENTION, bad, self.elapsed(), self.actions)
                continue
            if self.rng.random() < self.prof["up_prob"] and self.actions.count("scroll") > 2:
                self.scroll(-1)
            else:
                self.scroll(1)
            lo_m, hi_m = self.prof["mouse_moves"]
            for _ in range(self.rng.randint(lo_m, hi_m) if self.rng.random() < 0.6 else 0):
                self.wiggle()
            self.pause()
        return HumanResult(OK, "", self.elapsed(), self.actions)


def run_session(page, cfg: dict, base_url: str = "https://www.reddit.com", *, rng: random.Random | None = None,
                clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] | None = None,
                guard: WriteGuard | None = None) -> HumanResult:
    """Прогнать «живую» сессию на уже открытой странице залогиненного контекста. Не бросает исключений:
    любая ошибка -> needs_attention. Для записи cookies результат не важен — их сохраняет вызывающий."""
    rng = rng or random.Random()
    sleep = sleep or (lambda s: page.wait_for_timeout(int(s * 1000)))
    s = _Session(page, cfg, base_url, rng, clock, sleep)
    try:
        res = s.run()
    except Exception as e:  # noqa: BLE001 — сессия обслуживания не должна ронять джоб
        res = HumanResult(NEEDS_ATTENTION, f"{type(e).__name__}: {e}", s.elapsed(), s.actions)
    res.duration = s.elapsed()
    if guard is not None:
        res.blocked_writes = len(guard.blocked)
    return res


def make_guard(cfg: dict) -> WriteGuard:
    return WriteGuard(cfg.get("allow_post_hosts") or (), cfg.get("write_markers") or ())


def maintain(name: str, context, page, cfg: dict, *, base_url: str, storage_state_path: Path | None,
             cookies: list[dict], rng: random.Random | None = None, force: bool = False,
             state_dir: Path | None = None, log=None) -> HumanResult:
    """Полный цикл для одного аккаунта: проверка «пора ли и нужно ли» -> предохранитель -> сессия -> state.
    Вызывать только когда вход уже подтверждён (/api/me.json вернул data.name)."""
    rng = rng or random.Random()
    ok, why = should_run(name, cfg, storage_state_path, cookies, force=force, state_dir=state_dir)
    if not ok:
        return HumanResult(SKIPPED, why)
    guard = make_guard(cfg)
    guard.install(context)
    res = run_session(page, cfg, base_url, rng=rng, guard=guard)
    now = time.time()
    write_state(name, {"last_run_ts": now, "next_due_ts": next_due_ts(now, cfg, rng),
                       "status": res.status, "detail": res.detail, "duration": round(res.duration, 1),
                       "actions": len(res.actions), "blocked_writes": res.blocked_writes}, state_dir)
    if log:
        log.info("[%s] human-сессия: %s (%.0f с, действий %d, заблокировано записей %d)%s",
                 name, res.status, res.duration, len(res.actions), res.blocked_writes,
                 f" — {res.detail}" if res.detail else "")
    return res