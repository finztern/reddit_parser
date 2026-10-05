#!/usr/bin/env python3
"""
scripts/refresh_guest_cookies.py

Независимый Playwright-джоб — харвестит АНОНИМНЫЕ (гостевые) cookies для
гостевых слотов account_11..25 (секция guest_accounts в
scripts/refresh_guest_cookies.yaml).

В отличие от scripts/refresh_cookies.py (логин-аккаунты 1..10), логина
здесь нет: с мая 2026 Reddit блокирует .json без ЛЮБЫХ cookies (403), но
достаточно один раз зайти на reddit.com настоящим браузером и
переиспользовать анонимные cookies. Самая короткоживущая —
session_tracker (~2 часа), поэтому джоб рассчитан на частый запуск
(scripts/systemd/refresh-guest-cookies.timer).

Для каждого слота:

  1. Playwright-контекст: engine/channel/device — из guest_accounts;
     impersonate и user_agent — ИЗ accounts.yaml (источник правды: именно
     с ними скрапер ходит за comments.json). Расхождение с
     guest_accounts даёт WARNING, а не тихий рассинхрон отпечатков;
  2. заходит на reddit_base_url через тот же proxy_port, что и скрапер;
  3. wait_until=domcontentloaded (НЕ networkidle — на reddit.com он почти
     никогда не наступает и давал таймауты) + пауза post_load_settle;
  4. забирает cookies, оставляет домен reddit.com;
  5. ВЕРИФИЦИРУЕТ запросом comments.json через curl_cffi с тем же
     impersonate/прокси — только 200 считается успехом;
  6. успех -> АТОМАРНО (os.replace) пишет cookies/account_N.json
     (hot-reload в account_worker подхватит). Провал -> файл не трогает.

В конце прогона слоты, не прошедшие верификацию, повторяются
(retry_failed_passes) — иначе следующая попытка только через час, а
гостевая cookie живёт ~2ч.

Один браузерный процесс одновременно (последовательный цикл).

Установка:

    python3 -m venv venv
    venv/bin/pip install -r requirements-refresh.txt
    venv/bin/playwright install --with-deps chromium firefox webkit
    # только для слотов с channel: chrome / msedge:
    venv/bin/playwright install chrome msedge

Запуск:

    venv/bin/python3 scripts/refresh_guest_cookies.py
    venv/bin/python3 scripts/refresh_guest_cookies.py --account account_12
    venv/bin/python3 scripts/refresh_guest_cookies.py --no-headless
    venv/bin/python3 scripts/refresh_guest_cookies.py --dry-run
"""

import argparse
import copy
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
        "Playwright не установлен:\n"
        "  python3 -m venv venv\n"
        "  venv/bin/pip install -r requirements-refresh.txt\n"
        "  venv/bin/playwright install --with-deps chromium firefox webkit",
        file=sys.stderr,
    )
    raise

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("curl_cffi не установлен (нужен для верификации — см. requirements-refresh.txt).", file=sys.stderr)
    raise

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent))

from refresh_cookies import (  # noqa: E402
    _apply_stealth,
    atomic_write_json,
    infer_profile,
    only_reddit_cookies,
    playwright_to_cookie_editor,
)
from scraper.config import load_accounts  # noqa: E402
from scraper.constants import BASE_DIR, PROXY_HOST, log  # noqa: E402


# ---------------------------------------------------------------- #
#  Конфиг джоба
# ---------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "headless": True,
    "reddit_base_url": "https://www.reddit.com",
    "warmup_path": "/",
    "warmup_wait_until": "domcontentloaded",
    "warmup_page_load_timeout_seconds": 30,
    "warmup_retries": 2,
    "post_load_settle_seconds": 6,
    "verify_subreddit": "AskReddit",
    "verify_timeout_seconds": 20,
    "retry_failed_passes": 1,
    "stagger_seconds": 12,
    "geo_defaults": {
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "viewport": {"width": 1920, "height": 1080},
    },
    "account_geo": {},
    # slot -> {engine, channel, device, user_agent, impersonate, [geo]}
    "guest_accounts": {},
}


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_guest_config(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        log.info("%s не найден — дефолты джоба (guest_accounts пуст!)", path)
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    _deep_merge(cfg, user_cfg)
    return cfg


@dataclass
class AccountResult:
    name: str
    ok: bool
    detail: str


# ---------------------------------------------------------------- #
#  Один гостевой слот
# ---------------------------------------------------------------- #

def process_guest_account(pw, account: dict, cfg: dict, dry_run: bool) -> AccountResult:
    name = account["name"]
    cookie_file = BASE_DIR / account["cookie_file"]
    proxy_port = account.get("proxy_port")

    guest_spec = cfg["guest_accounts"].get(name)
    if not guest_spec:
        return AccountResult(name, False, "нет записи в guest_accounts — нечего харвестить")
    if not proxy_port:
        log.error("[%s] нет proxy_port в accounts.yaml — пропускаю", name)
        return AccountResult(name, False, "нет proxy_port в accounts.yaml")
    proxy_url = f"http://{PROXY_HOST}:{proxy_port}"

    # --- accounts.yaml — источник правды для отпечатка запросов ---
    impersonate = account.get("impersonate") or guest_spec.get("impersonate")
    user_agent = account.get("user_agent") or guest_spec.get("user_agent")
    for key, acc_val, spec_val in (
        ("impersonate", account.get("impersonate"), guest_spec.get("impersonate")),
        ("user_agent", account.get("user_agent"), guest_spec.get("user_agent")),
    ):
        if acc_val and spec_val and acc_val != spec_val:
            log.warning(
                "[%s] %s расходится: accounts.yaml=%r, guest_accounts=%r — использую accounts.yaml "
                "(синхронизируй файлы: scripts/rotate_fingerprints.py)", name, key, acc_val, spec_val,
            )
    if not impersonate:
        return AccountResult(name, False, "нужен impersonate (accounts.yaml / guest_accounts)")

    inferred = infer_profile(impersonate)
    engine = guest_spec.get("engine") or inferred["engine"]
    channel = guest_spec["channel"] if "channel" in guest_spec else inferred["channel"]
    device_name = guest_spec.get("device") or inferred["device"]
    if not user_agent and not device_name:
        return AccountResult(name, False, "нужен user_agent либо device")

    launcher = getattr(pw, engine, None)
    if launcher is None:
        return AccountResult(name, False, f"неизвестный engine={engine!r} (chromium/firefox/webkit)")

    # --- контекст ---
    geo = {**cfg["geo_defaults"], **cfg.get("account_geo", {}).get(name, {}), **(guest_spec.get("geo") or {})}
    context_kwargs: dict = {}
    if device_name:
        try:
            context_kwargs = dict(pw.devices[device_name])
        except KeyError:
            return AccountResult(name, False, f"нет device-пресета {device_name!r} в этой версии Playwright")
        expected_engine = context_kwargs.pop("default_browser_type", None)
        if expected_engine and expected_engine != engine:
            log.warning("[%s] device=%r рассчитан на %r, в конфиге engine=%r — оставляю конфиг",
                        name, device_name, expected_engine, engine)
        # viewport/is_mobile/has_touch берём из пресета устройства. Раньше
        # десктопный viewport 1920x1080 затирал мобильный у iPhone/Pixel —
        # "телефон" с экраном монитора.
    else:
        context_kwargs["viewport"] = geo.get("viewport") or cfg["geo_defaults"]["viewport"]
    if user_agent:
        context_kwargs["user_agent"] = user_agent

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
            proxy={"server": proxy_url},
            locale=geo.get("locale"),
            timezone_id=geo.get("timezone_id"),
            **context_kwargs,
        )
        try:
            page = context.new_page()
            _apply_stealth(page, engine)

            base_url = cfg["reddit_base_url"]
            url = base_url.rstrip("/") + cfg["warmup_path"]
            timeout_ms = int(cfg["warmup_page_load_timeout_seconds"] * 1000)
            wait_until = cfg.get("warmup_wait_until", "domcontentloaded")
            retries = max(1, int(cfg["warmup_retries"]))

            last_err = None
            loaded = False
            for attempt in range(1, retries + 1):
                try:
                    page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                    loaded = True
                    break
                except PlaywrightTimeoutError as e:
                    last_err = e
                    log.warning("[%s] попытка %d/%d: таймаут загрузки %s через %s: %s",
                                name, attempt, retries, url, proxy_url, e)
                except Exception as e:
                    last_err = e
                    log.warning("[%s] попытка %d/%d: ошибка навигации через %s (mihomo/порт %s живы?): %s",
                                name, attempt, retries, proxy_url, proxy_port, e)
                if attempt < retries:
                    time.sleep(3)
            if not loaded:
                return AccountResult(name, False, f"не удалось загрузить {url}: {last_err}")

            # page.wait_for_timeout, а не time.sleep: в sync-API Playwright
            # событийный цикл крутится только внутри вызовов Playwright.
            page.wait_for_timeout(int(cfg["post_load_settle_seconds"] * 1000))

            reddit_cookies = only_reddit_cookies(context.cookies())
            if not reddit_cookies:
                return AccountResult(name, False, "после загрузки не нашёл ни одной cookie домена reddit.com")

            rows = playwright_to_cookie_editor(reddit_cookies)
            cookie_dict = {c["name"]: c["value"] for c in rows if c.get("name")}

            verify_url = f"{base_url.rstrip('/')}/r/{cfg['verify_subreddit']}/comments.json"
            try:
                resp = cffi_requests.get(
                    verify_url,
                    params={"limit": 5},
                    cookies=cookie_dict,
                    impersonate=impersonate,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=cfg["verify_timeout_seconds"],
                )
            except Exception as e:
                return AccountResult(name, False, f"верификация ({verify_url}) упала: {e}")

            if resp.status_code != 200:
                return AccountResult(
                    name, False,
                    f"верификация: {verify_url} вернул {resp.status_code} "
                    f"(cookies: {', '.join(sorted(cookie_dict))}) — cookies не приняты Reddit'ом",
                )

            if dry_run:
                log.info("[%s] dry-run: %d cookies, верификация OK (%s)", name, len(rows), resp.status_code)
            else:
                atomic_write_json(cookie_file, rows)
                log.info("[%s] cookies обновлены: %s (%d шт.), верификация OK (%s) [%s/%s]",
                         name, cookie_file, len(rows), resp.status_code, engine, impersonate)

            return AccountResult(name, True, f"{len(rows)} cookies, verify={resp.status_code}")
        finally:
            context.close()
    finally:
        browser.close()


# ---------------------------------------------------------------- #
#  main
# ---------------------------------------------------------------- #

def _run_pass(pw, accounts: list[dict], cfg: dict, dry_run: bool) -> list[AccountResult]:
    results = []
    for i, account in enumerate(accounts):
        if i > 0 and cfg["stagger_seconds"] > 0:
            time.sleep(cfg["stagger_seconds"])
        try:
            result = process_guest_account(pw, account, cfg, dry_run)
        except Exception as e:
            log.exception("[%s] неожиданная ошибка обработки гостевого слота", account["name"])
            result = AccountResult(account["name"], False, f"необработанное исключение: {e}")
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", action="append", help="Только этот(и) гостевой(ые) слот(ы).")
    parser.add_argument("--headless", dest="headless", action="store_true", default=None)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Ничего не записывать в cookies/.")
    parser.add_argument(
        "--config", default=None,
        help="Путь к refresh_guest_cookies.yaml (по умолчанию scripts/refresh_guest_cookies.yaml, "
             "либо env REFRESH_GUEST_COOKIES_CONFIG)",
    )
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else Path(
        os.environ.get("REFRESH_GUEST_COOKIES_CONFIG", _SCRIPTS_DIR / "refresh_guest_cookies.yaml")
    )
    cfg = load_guest_config(config_path)
    if args.headless is not None:
        cfg["headless"] = args.headless

    guest_slot_names = set(cfg["guest_accounts"])
    if not guest_slot_names:
        log.error("guest_accounts пуст в %s — нечего харвестить", config_path)
        return 1

    all_accounts = {a["name"]: a for a in load_accounts()}
    wanted = set(args.account) if args.account else guest_slot_names
    if wanted - set(all_accounts):
        log.warning("Слотов нет в accounts.yaml: %s", ", ".join(sorted(wanted - set(all_accounts))))
    if wanted - guest_slot_names:
        log.warning("Слотов нет в guest_accounts (%s) — пропускаю (логин-аккаунты тут не трогаем): %s",
                    config_path, ", ".join(sorted(wanted - guest_slot_names)))

    def _num(n: str) -> int:
        return int(n.rsplit("_", 1)[-1]) if n.rsplit("_", 1)[-1].isdigit() else 0

    accounts = [all_accounts[n] for n in sorted(wanted & set(all_accounts) & guest_slot_names, key=_num)]
    if not accounts:
        log.error("Нет ни одного гостевого слота для обработки — проверь accounts.yaml / %s / --account", config_path)
        return 1

    log.info("refresh_guest_cookies: старт, %d слот(ов), headless=%s, stagger=%.0fs%s",
             len(accounts), cfg["headless"], cfg["stagger_seconds"], " [DRY-RUN]" if args.dry_run else "")

    final: dict[str, AccountResult] = {}
    with sync_playwright() as pw:
        pending = accounts
        for pass_no in range(1 + max(0, int(cfg["retry_failed_passes"]))):
            if not pending:
                break
            if pass_no > 0:
                log.info("повторный проход %d: %d слот(ов) не прошли верификацию", pass_no, len(pending))
                time.sleep(max(cfg["stagger_seconds"], 30))
            for r in _run_pass(pw, pending, cfg, args.dry_run):
                final[r.name] = r
            pending = [a for a in pending if not final[a["name"]].ok]

    ok = [r for r in final.values() if r.ok]
    failed = [r for r in final.values() if not r.ok]
    log.info("refresh_guest_cookies: завершено — успешно=%d, ошибок=%d%s",
             len(ok), len(failed), " [DRY-RUN]" if args.dry_run else "")
    for r in failed:
        log.error("  [%s] не удалось: %s", r.name, r.detail)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
