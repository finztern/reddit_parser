#!/usr/bin/env python3
"""
scripts/refresh_cookies.py

Независимый (от event loop/процессов scraper) Playwright-джоб. Раз в
запуск (расписание — снаружи, см. README.md: systemd timer/cron), для
каждого ЛОГИН-аккаунта (account_1..10, список — login_accounts в
refresh_cookies.yaml):

  - если в refresh_cookies.yaml задан список login_accounts — ровно для
    аккаунтов из этого списка (поле enabled в accounts.yaml при этом
    игнорируется — можно освежать сессию ещё выключенного аккаунта);
  - если список пуст — для всех enabled: true аккаунтов из accounts.yaml
    (старое поведение; опасно, если среди них есть гостевые слоты).

Для каждого аккаунта:

  1. открывает браузерный контекст через ТОТ ЖЕ proxy_port, что и сам
     скрапер для этого аккаунта (mihomo, тот же выходной IP);
  2. движок браузера выводится из impersonate аккаунта (firefoxNNN ->
     firefox, chromeNNN -> chromium, safariNNN -> webkit; можно
     переопределить через account_profiles в refresh_cookies.yaml) —
     раньше всегда запускался chromium, даже для firefox-аккаунтов;
  3. переиспользует сохранённую Playwright-сессию
     (storage_state/account_N.json), либо конвертирует на лету текущий
     cookies/account_N.json, если сохранённой сессии ещё нет;
  4. проверяет залогиненность по ПОЗИТИВНОМУ признаку (ответ
     авторизованного API-эндпоинта Reddit), а не по отсутствию формы
     логина в DOM;
  5. если НЕ залогинены — cookies/account_N.json НЕ трогает, логирует
     ERROR. Логин по паролю этот скрипт не автоматизирует — для первого
     логина используй scripts/manual_login.py;
  6. если залогинены — сохраняет storage_state/account_N.json и АТОМАРНО
     (os.replace()) перезаписывает cookies/account_N.json (только
     cookies домена reddit.com) в формате, который понимает
     scraper.config.load_cookies();
  7. сверяет фактический navigator.userAgent с accounts.yaml:user_agent —
     при расхождении WARNING (accounts.yaml не трогается).

Установка (отдельный venv, не requirements.txt основного скрапера):

    python3 -m venv venv
    venv/bin/pip install -r requirements-refresh.txt
    venv/bin/playwright install --with-deps chromium firefox webkit

Запуск:

    venv/bin/python3 scripts/refresh_cookies.py                    # все login_accounts
    venv/bin/python3 scripts/refresh_cookies.py --account account_1
    venv/bin/python3 scripts/refresh_cookies.py --no-headless
    venv/bin/python3 scripts/refresh_cookies.py --dry-run

Возвращает ненулевой exit code, если хотя бы один аккаунт требует
ручного вмешательства.
"""

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "Playwright не установлен. Установи зависимости джоба отдельно от основного "
        "requirements.txt:\n"
        "  python3 -m venv venv\n"
        "  venv/bin/pip install -r requirements-refresh.txt\n"
        "  venv/bin/playwright install --with-deps chromium firefox webkit",
        file=sys.stderr,
    )
    raise

try:
    from playwright_stealth import StealthConfig, stealth_sync
except ImportError:
    stealth_sync = None
    StealthConfig = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import load_accounts  # noqa: E402
from scraper.constants import BASE_DIR, PROXY_HOST, log  # noqa: E402


# ---------------------------------------------------------------- #
#  Конфиг джоба (refresh_cookies.yaml, опционально)
# ---------------------------------------------------------------- #

DEFAULT_CONFIG = {
    # Явный список логин-аккаунтов. Пусто -> все enabled: true из accounts.yaml.
    "login_accounts": [],
    "headless": True,
    "login_check_timeout_seconds": 20,
    "stagger_seconds": 45,
    "storage_state_dir": "storage_state",
    "reddit_base_url": "https://www.reddit.com",
    "login_check_path": "/api/me.json",
    "geo_defaults": {
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "viewport": {"width": 1920, "height": 1080},
    },
    # гео = геолокация VLESS-ноды аккаунта (см. vless_accounts.env)
    "account_geo": {
        "account_1": {"locale": "sr-RS", "timezone_id": "Europe/Belgrade"},
        "account_2": {"locale": "bg-BG", "timezone_id": "Europe/Sofia"},
        "account_3": {"locale": "cs-CZ", "timezone_id": "Europe/Prague"},
        "account_4": {"locale": "ja-JP", "timezone_id": "Asia/Tokyo"},
        "account_5": {"locale": "en-SG", "timezone_id": "Asia/Singapore"},
        "account_6": {"locale": "hu-HU", "timezone_id": "Europe/Budapest"},
        "account_7": {"locale": "nl-BE", "timezone_id": "Europe/Brussels"},
        "account_8": {"locale": "sq-AL", "timezone_id": "Europe/Tirane"},
        "account_9": {"locale": "uk-UA", "timezone_id": "Europe/Kiev"},
        "account_10": {"locale": "de-DE", "timezone_id": "Europe/Berlin"},
    },
    # name -> {engine, channel, device}; перекрывает вывод из impersonate
    "account_profiles": {},
}


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_refresh_config(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        log.info("%s не найден — использую дефолты джоба", path)
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    # Альтернатива: секция `refresh_cookies:` внутри общего config.yaml.
    top_level_keys = set(DEFAULT_CONFIG) - {"refresh_cookies"}
    if "refresh_cookies" in user_cfg and not (top_level_keys & user_cfg.keys()):
        user_cfg = user_cfg["refresh_cookies"]
    _deep_merge(cfg, user_cfg)
    return cfg


# ---------------------------------------------------------------- #
#  Профиль браузера из impersonate
# ---------------------------------------------------------------- #

def infer_profile(impersonate: str | None) -> dict:
    """impersonate (curl_cffi) -> engine/channel/device Playwright."""
    imp = (impersonate or "").lower()
    if imp.startswith("firefox"):
        return {"engine": "firefox", "channel": None, "device": None}
    if imp.startswith("safari"):
        return {"engine": "webkit", "channel": None, "device": "iPhone 14" if "ios" in imp else None}
    if imp.startswith("edge"):
        return {"engine": "chromium", "channel": "msedge", "device": None}
    if "android" in imp:
        return {"engine": "chromium", "channel": None, "device": "Pixel 7"}
    return {"engine": "chromium", "channel": None, "device": None}


# ---------------------------------------------------------------- #
#  Конвертация форматов cookies: Cookie Editor <-> Playwright
# ---------------------------------------------------------------- #

def cookie_editor_to_playwright(raw_cookies: list[dict], default_domain: str = ".reddit.com") -> list[dict]:
    """Cookie Editor export (name/value [+ прочие поля]) -> формат
    Playwright storage_state (domain и path обязательны)."""
    out = []
    for c in raw_cookies:
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            continue
        same_site = c.get("sameSite") or "Lax"
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        expires = c.get("expirationDate", c.get("expires"))
        try:
            expires_f = float(expires) if expires not in (None, "", -1) else -1
        except (TypeError, ValueError):
            expires_f = -1
        out.append(
            {
                "name": name,
                "value": value,
                "domain": c.get("domain") or default_domain,
                "path": c.get("path") or "/",
                "expires": expires_f,
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": same_site,
            }
        )
    return out


def playwright_to_cookie_editor(pw_cookies: list[dict]) -> list[dict]:
    """Обратная конвертация — формат для scraper.config.load_cookies()
    (нужны только name/value, остальное — для наглядности)."""
    out = []
    for c in pw_cookies:
        out.append(
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expirationDate": c.get("expires", -1),
                "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", True),
                "sameSite": c.get("sameSite", "Lax"),
            }
        )
    return out


def only_reddit_cookies(pw_cookies: list[dict]) -> list[dict]:
    """Скрапер собирает cookies в dict по имени — cookie чужих доменов
    (google и т.п.) могут затереть одноимённые reddit-овские."""
    return [c for c in pw_cookies if "reddit.com" in (c.get("domain") or "")]


# ---------------------------------------------------------------- #
#  Атомарная запись (account_worker читает cookies/account_N.json
#  параллельно через hot-reload и не должен увидеть частичный файл)
# ---------------------------------------------------------------- #

def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


# ---------------------------------------------------------------- #
#  Stealth — только для chromium.
#
#  Раньше патчи применялись ко всем движкам: window.chrome и поддельные
#  navigator.plugins в firefox/webkit — это не маскировка, а явный
#  признак подделки. Поддельные navigator.languages = ['en-US', 'en']
#  противоречили locale из account_geo (Accept-Language другой).
# ---------------------------------------------------------------- #

_MANUAL_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


def _apply_stealth(page, engine: str = "chromium") -> None:
    if engine != "chromium":
        return
    if stealth_sync is not None:
        try:
            if StealthConfig is not None:
                config = StealthConfig()
                # не перетираем languages/platform/webgl фиксированными
                # значениями — они не совпали бы с UA/locale аккаунта
                for attr in ("navigator_languages", "navigator_platform", "webgl_vendor"):
                    if hasattr(config, attr):
                        setattr(config, attr, False)
                stealth_sync(page, config)
            else:
                stealth_sync(page)
            return
        except Exception as e:
            log.warning("playwright-stealth упал (%s) — продолжаю с ручным патчем", e)
    page.add_init_script(_MANUAL_STEALTH_INIT_SCRIPT)


# ---------------------------------------------------------------- #
#  Проверка залогиненности
# ---------------------------------------------------------------- #

def _is_logged_in(page, base_url: str, login_check_path: str, timeout_ms: int) -> tuple[bool, str]:
    url = base_url.rstrip("/") + login_check_path
    try:
        resp = page.request.get(url, timeout=timeout_ms)
    except Exception as e:
        return False, f"запрос к {login_check_path} упал: {e}"
    if resp.status != 200:
        return False, f"{login_check_path} вернул статус {resp.status}"
    try:
        data = resp.json()
    except Exception as e:
        return False, f"{login_check_path} вернул не-JSON: {e}"
    user_data = data.get("data") if isinstance(data, dict) else None
    if isinstance(user_data, dict) and user_data.get("name"):
        return True, f"залогинен как {user_data.get('name')}"
    return False, f"{login_check_path} без data.name — сессия не авторизована"


# ---------------------------------------------------------------- #
#  Обработка одного аккаунта
# ---------------------------------------------------------------- #

@dataclass
class AccountResult:
    name: str
    ok: bool
    detail: str


def process_account(pw, account: dict, cfg: dict, dry_run: bool) -> AccountResult:
    name = account["name"]
    cookie_file = BASE_DIR / account["cookie_file"]
    proxy_port = account.get("proxy_port")
    configured_ua = account.get("user_agent")

    if not proxy_port:
        log.error("[%s] нет proxy_port в accounts.yaml — пропускаю (логин с чужого IP — риск для аккаунта)", name)
        return AccountResult(name, False, "нет proxy_port в accounts.yaml")
    proxy_url = f"http://{PROXY_HOST}:{proxy_port}"

    profile = {**infer_profile(account.get("impersonate")), **(cfg.get("account_profiles", {}).get(name) or {})}
    engine = profile.get("engine") or "chromium"
    channel = profile.get("channel")
    device_name = profile.get("device")

    launcher = getattr(pw, engine, None)
    if launcher is None:
        return AccountResult(name, False, f"неизвестный engine={engine!r}")

    storage_state_dir = BASE_DIR / cfg["storage_state_dir"]
    storage_state_dir.mkdir(parents=True, exist_ok=True)
    storage_state_path = storage_state_dir / f"{name}.json"

    geo = {**cfg["geo_defaults"], **cfg.get("account_geo", {}).get(name, {})}

    if storage_state_path.exists():
        storage_state = str(storage_state_path)
        log.info("[%s] использую сохранённый storage_state: %s", name, storage_state_path)
    elif cookie_file.exists():
        try:
            raw = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("[%s] не удалось прочитать %s для инициализации storage_state: %s", name, cookie_file, e)
            raw = []
        storage_state = {"cookies": cookie_editor_to_playwright(raw), "origins": []}
        log.info("[%s] storage_state ещё нет — инициализирую из %s", name, cookie_file)
    else:
        log.warning(
            "[%s] нет ни %s, ни сохранённого storage_state — пустая сессия "
            "(нужен ручной логин: scripts/manual_login.py %s)", name, cookie_file, name,
        )
        storage_state = {"cookies": [], "origins": []}

    context_kwargs: dict = {}
    if device_name:
        try:
            context_kwargs = dict(pw.devices[device_name])
        except KeyError:
            return AccountResult(name, False, f"нет device-пресета {device_name!r} в этой версии Playwright")
        context_kwargs.pop("default_browser_type", None)
    else:
        context_kwargs["viewport"] = geo.get("viewport") or cfg["geo_defaults"]["viewport"]
    if configured_ua:
        context_kwargs["user_agent"] = configured_ua

    launch_kwargs: dict = {"headless": cfg["headless"]}
    if channel:
        launch_kwargs["channel"] = channel
    if engine == "chromium":
        launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]

    try:
        browser = launcher.launch(**launch_kwargs)
    except Exception as e:
        return AccountResult(
            name, False,
            f"не смог запустить {engine}(channel={channel!r}): {e} (playwright install {channel or engine})",
        )

    try:
        context = browser.new_context(
            storage_state=storage_state,
            proxy={"server": proxy_url},
            locale=geo.get("locale"),
            timezone_id=geo.get("timezone_id"),
            **context_kwargs,
        )
        try:
            page = context.new_page()
            _apply_stealth(page, engine)

            base_url = cfg["reddit_base_url"]
            login_check_path = cfg["login_check_path"]
            timeout_ms = int(cfg["login_check_timeout_seconds"] * 1000)

            try:
                page.goto(base_url, timeout=timeout_ms, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                log.error("[%s] не удалось открыть %s через %s за %sмс: %s", name, base_url, proxy_url, timeout_ms, e)
                return AccountResult(name, False, f"навигация не уложилась в таймаут: {e}")
            except Exception as e:
                log.error("[%s] ошибка навигации/сети через %s (проверь mihomo/порт %s): %s", name, proxy_url, proxy_port, e)
                return AccountResult(name, False, f"сеть/навигация: {e}")

            logged_in, detail = _is_logged_in(page, base_url, login_check_path, timeout_ms)
            if not logged_in:
                log.error(
                    "[%s] сессия разлогинена (%s) — cookies/%s.json НЕ трогаю, нужен ручной логин "
                    "(scripts/manual_login.py %s)", name, detail, name, name,
                )
                return AccountResult(name, False, detail)

            log.info("[%s] сессия активна (%s) [engine=%s]", name, detail, engine)

            try:
                actual_ua = page.evaluate("navigator.userAgent")
            except Exception:
                actual_ua = None
            if configured_ua and actual_ua and actual_ua != configured_ua:
                log.warning(
                    "[%s] User-Agent браузера разошёлся с accounts.yaml (accounts.yaml не трогаю): "
                    "accounts.yaml=%r, фактический=%r", name, configured_ua, actual_ua,
                )

            new_storage_state = context.storage_state()
            new_cookies = only_reddit_cookies(new_storage_state.get("cookies", []))

            if not new_cookies:
                return AccountResult(name, False, "после проверки нет ни одной cookie reddit.com — файл не трогаю")

            if dry_run:
                log.info("[%s] dry-run: cookies/storage_state НЕ записаны (%d cookies reddit.com)", name, len(new_cookies))
            else:
                atomic_write_json(storage_state_path, new_storage_state)
                atomic_write_json(cookie_file, playwright_to_cookie_editor(new_cookies))
                log.info("[%s] cookies обновлены: %s (%d шт.), storage_state: %s",
                         name, cookie_file, len(new_cookies), storage_state_path)

            return AccountResult(name, True, detail)
        finally:
            context.close()
    finally:
        browser.close()


# ---------------------------------------------------------------- #
#  main
# ---------------------------------------------------------------- #

def select_accounts(cfg: dict, only: list[str] | None) -> list[dict]:
    all_accounts = load_accounts()
    login_accounts = cfg.get("login_accounts") or []

    if login_accounts:
        by_name = {a["name"]: a for a in all_accounts}
        unknown = [n for n in login_accounts if n not in by_name]
        if unknown:
            log.warning("login_accounts: нет в accounts.yaml: %s", ", ".join(unknown))
        accounts = [by_name[n] for n in login_accounts if n in by_name]
        source = "login_accounts"
    else:
        accounts = [a for a in all_accounts if a.get("enabled")]
        source = "enabled: true в accounts.yaml"
        log.warning("login_accounts пуст — беру ВСЕ enabled аккаунты (в т.ч. возможные гостевые слоты!)")

    if only:
        wanted = set(only)
        accounts = [a for a in accounts if a["name"] in wanted]
        missing = wanted - {a["name"] for a in accounts}
        if missing:
            log.warning("Аккаунты не найдены среди %s: %s", source, ", ".join(sorted(missing)))

    return accounts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", action="append", help="Только этот(и) аккаунт(ы) (флаг можно повторять).")
    parser.add_argument("--headless", dest="headless", action="store_true", default=None)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Ничего не записывать в cookies/storage_state.")
    parser.add_argument(
        "--config", default=None,
        help="Путь к refresh_cookies.yaml (по умолчанию: scripts/refresh_cookies.yaml, "
             "либо env REFRESH_COOKIES_CONFIG)",
    )
    args = parser.parse_args()

    # Дефолт раньше указывал в корень проекта (BASE_DIR/refresh_cookies.yaml),
    # хотя файл лежит в scripts/ — и без --config джоб молча работал на дефолтах.
    default_cfg = Path(__file__).resolve().parent / "refresh_cookies.yaml"
    config_path = Path(args.config) if args.config else Path(
        os.environ.get("REFRESH_COOKIES_CONFIG", default_cfg)
    )
    cfg = load_refresh_config(config_path)
    if args.headless is not None:
        cfg["headless"] = args.headless

    accounts = select_accounts(cfg, args.account)
    if not accounts:
        log.error("Нет ни одного аккаунта для обработки — проверь login_accounts / accounts.yaml / --account")
        return 1

    log.info(
        "refresh_cookies: старт, %d аккаунт(ов), headless=%s, stagger=%.0fs, storage_state_dir=%s%s",
        len(accounts), cfg["headless"], cfg["stagger_seconds"], cfg["storage_state_dir"],
        " [DRY-RUN]" if args.dry_run else "",
    )

    results: list[AccountResult] = []
    with sync_playwright() as pw:
        for i, account in enumerate(accounts):
            if i > 0 and cfg["stagger_seconds"] > 0:
                time.sleep(cfg["stagger_seconds"])
            try:
                result = process_account(pw, account, cfg, args.dry_run)
            except Exception as e:
                log.exception("[%s] неожиданная ошибка обработки аккаунта", account["name"])
                result = AccountResult(account["name"], False, f"необработанное исключение: {e}")
            results.append(result)

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    log.info(
        "refresh_cookies: завершено — успешно=%d, требуют_вмешательства=%d%s",
        len(ok), len(failed), " [DRY-RUN, ничего не записано]" if args.dry_run else "",
    )
    for r in failed:
        log.error("  [%s] требует ручного вмешательства: %s", r.name, r.detail)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
