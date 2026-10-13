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

  0. если слот сейчас в ротации (заявка pending/working в state/requests)
     — пропускается; если после ротации у слота есть активный профиль
     (state/slots/account_N.json) — он ПЕРЕКРЫВАЕТ accounts.yaml и
     guest_accounts (иначе cookies харвестились бы со старым отпечатком);
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

Параллельность: слоты раздаются по N воркерам (--workers, по умолчанию
считается автоматически из свободной RAM и числа CPU, потолок —
parallel_browsers в refresh_guest_cookies.yaml). Каждый воркер — отдельный
процесс этого же скрипта со своим Playwright и СВОИМ подмножеством слотов
(cookies/account_N.json у воркеров не пересекаются). Перед запуском каждого
браузера воркер ждёт, пока MemAvailable >= min_free_memory_mb, иначе слот
помечается неудачным и повторяется в повторном проходе (защита от OOM).
--workers 1 — прежний последовательный режим в одном процессе.

Установка:

    python3 -m venv venv
    venv/bin/pip install -r requirements-refresh.txt
    venv/bin/playwright install --with-deps chromium firefox webkit
    # только для слотов с channel: chrome / msedge:
    venv/bin/playwright install chrome msedge

Запуск:

    venv/bin/python3 scripts/refresh_guest_cookies.py
    venv/bin/python3 scripts/refresh_guest_cookies.py --account account_12
    venv/bin/python3 scripts/refresh_guest_cookies.py --workers 4
    venv/bin/python3 scripts/refresh_guest_cookies.py --workers 1     # последовательно
    venv/bin/python3 scripts/refresh_guest_cookies.py --no-headless
    venv/bin/python3 scripts/refresh_guest_cookies.py --dry-run
"""

import argparse
import copy
import os
import signal
import subprocess
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
from scraper import identity as slot_state  # noqa: E402
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
    # --- параллельный харвест ---
    # Потолок одновременных браузеров (воркеров). Реальное число =
    # min(parallel_browsers, CPU, (MemAvailable - memory_reserve_mb) //
    # browser_memory_mb, число слотов); --workers N задаёт его явно.
    "parallel_browsers": 6,
    "browser_memory_mb": 600,       # консервативная оценка: браузер + node-драйвер Playwright
    "memory_reserve_mb": 1000,      # запас RAM под хост, mihomo, scraper
    "min_free_memory_mb": 700,      # воркер не стартует браузер, пока MemAvailable ниже
    "memory_wait_seconds": 120,     # сколько ждать освобождения RAM, потом слот -> неудача
    "worker_start_stagger_seconds": 4,  # пауза между стартами воркеров (пик CPU)
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
#  Ротация идентичностей: учёт активных профилей слотов
# ---------------------------------------------------------------- #

def apply_rotation_overrides(accounts: list[dict], cfg: dict) -> list[dict]:
    """Слот в ротации (заявка pending/working) пропускаем. Если у слота
    есть активный профиль после ротации (state/slots/account_N.json) —
    он перекрывает accounts.yaml и guest_accounts: харвестим cookies с тем
    же отпечатком и гео, с которыми слот реально работает. Прокси-порт тот
    же — демон ротации уже переключил в нём ноду."""
    kept = []
    for a in accounts:
        name = a["name"]
        req = slot_state.read_request(name)
        if req and req.get("status") in ("pending", "working"):
            log.info("[%s] идёт ротация идентичности — пропускаю в этом проходе", name)
            continue
        prof = slot_state.read_slot_profile(name)
        if prof:
            a = {**a, "impersonate": prof["impersonate"],
                 "user_agent": prof.get("user_agent") or a.get("user_agent")}
            cfg["guest_accounts"][name] = {
                k: prof[k] for k in ("engine", "channel", "device", "user_agent", "impersonate")
                if k in prof
            }
            if prof.get("geo"):
                cfg.setdefault("account_geo", {})[name] = prof["geo"]
            log.info("[%s] профиль после ротации: %s, нода %s", name, prof["impersonate"], prof.get("node"))
        kept.append(a)
    return kept


# ---------------------------------------------------------------- #
#  Память и параллельный режим
# ---------------------------------------------------------------- #

ENGINE_WEIGHT = {"chromium": 1.0, "firefox": 1.3, "webkit": 1.2}


def read_mem_available_mb() -> int | None:
    """MemAvailable из /proc/meminfo в МБ (None — не Linux / не прочитали)."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def wait_for_memory(cfg: dict, name: str) -> bool:
    """Ждёт, пока свободной RAM станет не меньше min_free_memory_mb.
    False — не дождались за memory_wait_seconds (слот пойдёт в повтор)."""
    need = int(cfg.get("min_free_memory_mb") or 0)
    if need <= 0:
        return True
    deadline = time.monotonic() + float(cfg.get("memory_wait_seconds") or 0)
    warned = False
    while True:
        avail = read_mem_available_mb()
        if avail is None or avail >= need:
            return True
        if not warned:
            log.warning("[%s] мало свободной RAM (%d МБ < %d МБ) — жду до %.0fс",
                        name, avail, need, float(cfg.get("memory_wait_seconds") or 0))
            warned = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)


def pick_worker_count(requested: int | None, n_slots: int, cfg: dict,
                      mem_mb: int | None, cpus: int | None) -> int:
    """requested: None/0 — авто, N>0 — ровно N (но не больше числа слотов)."""
    if n_slots <= 0:
        return 1
    if requested:
        return max(1, min(int(requested), n_slots))
    cap = max(1, int(cfg.get("parallel_browsers") or 1))
    by_cpu = max(1, cpus or 1)
    if mem_mb is None:
        by_mem = cap
    else:
        per = max(1, int(cfg.get("browser_memory_mb") or 600))
        reserve = max(0, int(cfg.get("memory_reserve_mb") or 0))
        by_mem = max(1, (mem_mb - reserve) // per)
    return max(1, min(cap, by_cpu, by_mem, n_slots))


def _slot_engine(account: dict, cfg: dict) -> str:
    spec = cfg["guest_accounts"].get(account["name"]) or {}
    if spec.get("engine"):
        return spec["engine"]
    imp = account.get("impersonate") or spec.get("impersonate")
    try:
        return infer_profile(imp)["engine"]
    except Exception:
        return "chromium"


def split_shards(accounts: list[dict], cfg: dict, n: int) -> list[list[dict]]:
    """Раскладывает слоты по n воркерам жадно (самые тяжёлые — первыми, в
    самый лёгкий воркер): firefox/webkit тяжелее chromium, поэтому
    round-robin по номеру дал бы перекос по RAM. Пустые воркеры не
    возвращаются; внутри воркера порядок — по номеру слота."""
    n = max(1, min(n, len(accounts)))
    shards: list[list[dict]] = [[] for _ in range(n)]
    load = [0.0] * n
    ordered = sorted(accounts, key=lambda a: (-ENGINE_WEIGHT.get(_slot_engine(a, cfg), 1.0), _acc_num(a["name"])))
    for a in ordered:
        i = min(range(n), key=lambda k: (load[k], k))
        shards[i].append(a)
        load[i] += ENGINE_WEIGHT.get(_slot_engine(a, cfg), 1.0)
    for sh in shards:
        sh.sort(key=lambda a: _acc_num(a["name"]))
    return [sh for sh in shards if sh]


def _acc_num(name: str) -> int:
    tail = name.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def run_parallel(shards: list[list[dict]], cfg: dict, config_path: Path, dry_run: bool) -> int:
    """Запускает по подпроцессу на шард (этот же скрипт, --workers 1) и ждёт
    всех. Код возврата: 0 — все слоты у всех воркеров успешны."""
    script = str(Path(__file__).resolve())
    procs: list[subprocess.Popen] = []

    def _terminate(*_):
        for pr in procs:
            if pr.poll() is None:
                pr.terminate()

    old_term = signal.signal(signal.SIGTERM, lambda *a: (_terminate(), sys.exit(143)))
    try:
        for i, shard in enumerate(shards):
            if i > 0 and cfg["worker_start_stagger_seconds"] > 0:
                time.sleep(cfg["worker_start_stagger_seconds"])
            cmd = [sys.executable, script, "--workers", "1", "--config", str(config_path),
                   "--headless" if cfg["headless"] else "--no-headless"]
            if dry_run:
                cmd.append("--dry-run")
            for a in shard:
                cmd += ["--account", a["name"]]
            env = {**os.environ, "GUEST_SHARD": f"{i + 1}/{len(shards)}"}
            log.info("воркер %d/%d: %d слот(ов): %s", i + 1, len(shards), len(shard),
                     ", ".join(a["name"] for a in shard))
            procs.append(subprocess.Popen(cmd, env=env))
        rc = 0
        for pr in procs:
            if pr.wait() != 0:
                rc = 1
        return rc
    except KeyboardInterrupt:
        _terminate()
        raise
    finally:
        signal.signal(signal.SIGTERM, old_term)


# ---------------------------------------------------------------- #
#  main
# ---------------------------------------------------------------- #

def _run_pass(pw, accounts: list[dict], cfg: dict, dry_run: bool) -> list[AccountResult]:
    results = []
    for i, account in enumerate(accounts):
        if i > 0 and cfg["stagger_seconds"] > 0:
            time.sleep(cfg["stagger_seconds"])
        if not wait_for_memory(cfg, account["name"]):
            results.append(AccountResult(account["name"], False,
                                         "мало свободной RAM — слот отложен до повторного прохода"))
            continue
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
        "--workers", type=int, default=None,
        help="Сколько браузеров параллельно (воркер = процесс). По умолчанию — авто по RAM/CPU "
             "(потолок parallel_browsers в yaml); 1 — последовательно в одном процессе.",
    )
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
    accounts = apply_rotation_overrides(accounts, cfg)
    if not accounts:
        if os.environ.get("GUEST_SHARD"):
            log.info("воркер %s: все его слоты сейчас в ротации — нечего делать", os.environ["GUEST_SHARD"])
            return 0
        log.error("Нет ни одного гостевого слота для обработки — проверь accounts.yaml / %s / --account "
                  "(или все слоты сейчас в ротации)", config_path)
        return 1

    shard_tag = os.environ.get("GUEST_SHARD")  # выставляет родитель в run_parallel
    workers = 1 if shard_tag else pick_worker_count(
        args.workers, len(accounts), cfg, read_mem_available_mb(), os.cpu_count())
    if workers > 1:
        shards = split_shards(accounts, cfg, workers)
        log.info("refresh_guest_cookies: %d слот(ов) -> %d параллельных воркер(ов) "
                 "(RAM свободно: %s МБ, CPU: %s, потолок parallel_browsers=%s)%s",
                 len(accounts), len(shards), read_mem_available_mb(), os.cpu_count(),
                 cfg["parallel_browsers"], " [DRY-RUN]" if args.dry_run else "")
        rc = run_parallel(shards, cfg, config_path, args.dry_run)
        log.info("refresh_guest_cookies: все воркеры завершились, код=%d", rc)
        return rc

    log.info("refresh_guest_cookies: старт%s, %d слот(ов), headless=%s, stagger=%.0fs%s",
             f" [воркер {shard_tag}]" if shard_tag else "",
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
