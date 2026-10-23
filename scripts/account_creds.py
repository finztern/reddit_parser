#!/usr/bin/env python3
"""
scripts/account_creds.py — ник и пароль для РУЧНОЙ регистрации аккаунта (login_hub.py / manual_login.py --register).

Что делает:
  * придумывает правдоподобный ник (Reddit: 3–20 символов, буквы/цифры/_/-) и сильный пароль (secrets);
  * проверяет, что ник свободен (публичный запрос username_available через прокси слота; если проверка не
    удалась — ник берётся без неё);
  * сохраняет пару в credentials/<account>.json (каталог 0700, файл 0600, атомарно): повторный запуск для того
    же аккаунта берёт ТЕ ЖЕ данные, чтобы регистрация не ушла в никуда после обрыва окна;
  * в окне регистрации (/register) подставляет ник и пароль в пустые поля — best-effort, разметка Reddit
    меняется; не получилось — данные напечатаны в консоли хаба.

Чего НЕ делает: не нажимает «зарегистрироваться», не вводит почту, не решает капчу и не подтверждает почту —
это делает человек в окне noVNC. Пароль печатается один раз в консоль оператора и лежит в credentials/ (не
в state/, не в логах, не в status.json). Каталог credentials/ в .gitignore/.dockerignore.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.constants import BASE_DIR  # noqa: E402

CREDS_DIR = BASE_DIR / "credentials"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")

ADJECTIVES = [
    "Quiet", "Brave", "Calm", "Clever", "Curious", "Eager", "Gentle", "Happy", "Lucky", "Mellow", "Misty", "Noble",
    "Polite", "Proud", "Rapid", "Silent", "Sunny", "Swift", "Tidy", "Witty", "Amber", "Azure", "Crimson", "Golden",
    "Silver", "Rustic", "Cosmic", "Lunar", "Solar", "Frosty", "Dusty", "Breezy", "Cozy", "Daring", "Fuzzy", "Jolly",
    "Keen", "Lively", "Modest", "Nimble", "Plucky", "Sleepy", "Steady", "Vivid", "Wandering", "Zesty", "Ancient",
    "Hidden", "Little", "Northern",
]
NOUNS = [
    "Falcon", "Otter", "Maple", "River", "Harbor", "Meadow", "Pebble", "Comet", "Lantern", "Willow", "Badger",
    "Heron", "Cedar", "Canyon", "Ember", "Fjord", "Glacier", "Hazel", "Island", "Juniper", "Kestrel", "Lagoon",
    "Marmot", "Nebula", "Orchid", "Puffin", "Quartz", "Raven", "Sparrow", "Tundra", "Violet", "Walrus", "Yarrow",
    "Zephyr", "Acorn", "Beacon", "Cricket", "Dune", "Fern", "Grove", "Hollow", "Iris", "Kayak", "Lynx", "Mesa",
    "Newt", "Owl", "Prairie", "Reef", "Summit",
]
_LOWER, _UPPER, _DIGITS, _SYMBOLS = "abcdefghijkmnpqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789", "-_!#%*+=?"


def make_username(rng=None) -> str:
    rng = rng or secrets.SystemRandom()
    for _ in range(50):
        a, n, d = rng.choice(ADJECTIVES), rng.choice(NOUNS), str(rng.randrange(10, 9999))
        name = rng.choice((f"{a}_{n}", f"{a}{n}{d}", f"{n}_{a}_{d}", f"{a}-{n}-{d}"))
        if USERNAME_RE.fullmatch(name):
            return name
    return "user_" + secrets.token_hex(5)


def make_password(length: int = 16, rng=None) -> str:
    """Без похожих символов (0/O, 1/l/I) — пароль можно прочитать из консоли и набрать руками."""
    rng = rng or secrets.SystemRandom()
    length = max(12, length)
    chars = [rng.choice(_LOWER), rng.choice(_UPPER), rng.choice(_DIGITS), rng.choice(_SYMBOLS)]
    pool = _LOWER + _UPPER + _DIGITS + _SYMBOLS
    chars += [rng.choice(pool) for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def _http_get(url: str, proxy_url: str | None, impersonate: str, timeout: float) -> tuple[int, str]:
    from curl_cffi import requests as cr
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    r = cr.get(url, proxies=proxies, impersonate=impersonate or "chrome", timeout=timeout)
    return r.status_code, r.text


def username_available(name: str, proxy_url: str | None, impersonate: str = "chrome",
                       timeout: float = 15.0) -> bool | None:
    """True — свободен, False — занят, None — проверить не удалось (сеть/403/неожиданный ответ)."""
    try:
        status, text = _http_get(f"https://www.reddit.com/api/username_available.json?user={name}",
                                 proxy_url, impersonate, timeout)
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    word = text.strip().lower()
    return True if word == "true" else False if word == "false" else None


def creds_path(account: str, base: Path | None = None) -> Path:
    return (base or CREDS_DIR) / f"{account}.json"


def load_credentials(account: str, base: Path | None = None) -> dict | None:
    try:
        data = json.loads(creds_path(account, base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("username") and data.get("password") else None


def save_credentials(account: str, data: dict, base: Path | None = None) -> Path:
    path = creds_path(account, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def prepare(account: str, proxy_url: str | None, impersonate: str = "chrome", *, new: bool = False,
            base: Path | None = None, tries: int = 8) -> tuple[dict, bool]:
    """-> (данные, повторно_использованы). Уже сгенерированные данные аккаунта берутся как есть (new=True —
    сгенерировать заново). Занятые ники пропускаются."""
    if not new:
        cur = load_credentials(account, base)
        if cur:
            return cur, True
    checked = None
    for _ in range(tries):
        name = make_username()
        checked = username_available(name, proxy_url, impersonate)
        if checked is not False:
            break
    data = {"account": account, "username": name, "password": make_password(), "status": "generated",
            "username_checked": checked, "created_at": int(time.time())}
    save_credentials(account, data, base)
    return data, False


def mark_logged_in(account: str, reddit_name: str, base: Path | None = None) -> None:
    """После входа: фактический ник (человек мог его сменить) и статус."""
    cur = load_credentials(account, base)
    if not cur:
        return
    cur.update(username=reddit_name or cur["username"], status="logged_in", logged_in_at=int(time.time()))
    save_credentials(account, cur, base)


FIELD_SELECTORS = {
    "username": ('input[name="username"]', "#register-username", 'faceplate-text-input[name="username"] input'),
    "password": ('input[name="password"]', "#register-password", 'faceplate-text-input[name="password"] input'),
}


def prefill_signup(page, creds: dict, filled: set[str]) -> list[str]:
    """Подставляет ник/пароль в ПУСТЫЕ видимые поля формы регистрации (только на /register; каждое поле — один
    раз, чтобы не мешать человеку). Ничего не отправляет. -> какие поля заполнены в этот вызов."""
    done: list[str] = []
    try:
        if "/register" not in (page.url or ""):
            return done
    except Exception:  # noqa: BLE001
        return done
    for field, selectors in FIELD_SELECTORS.items():
        if field in filled:
            continue
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                if loc.input_value():
                    filled.add(field)
                else:
                    loc.fill(creds[field])
                    filled.add(field)
                    done.append(field)
                break
            except Exception:  # noqa: BLE001 — разметка менялась/поле исчезло: пробуем следующий селектор
                continue
    return done
