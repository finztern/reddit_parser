#!/usr/bin/env python3
"""
scripts/manual_login.py — РУЧНОЙ логин в аккаунт Reddit. Запускается НА СЕРВЕРЕ.

Поднимает виртуальный экран (Xvfb) + VNC (x11vnc) + веб-морду (noVNC/
websockify), запускает на этом экране ВИДИМЫЙ Playwright-браузер с
профилем аккаунта (engine / UA / device / locale / timezone) и тем же
прокси-портом mihomo, что использует скрапер (http://PROXY_HOST:proxy_port
— тот же выходной IP). Ты открываешь ссылку из вывода в своём браузере
на домашнем ПК, видишь экран и логинишься руками.

Как только /api/me.json вернёт data.name (логин состоялся), скрипт:
  1. забирает storage_state контекста,
  2. проверяет cookies реальным запросом comments.json (curl_cffi, тот же
     impersonate и прокси),
  3. атомарно (os.replace) пишет cookies/account_N.json (hot-reload в
     account_worker подхватит сам) и storage_state/account_N.json (для
     scripts/refresh_cookies.py),
  4. гасит браузер и виртуальный экран.

Зависимости на сервере (один раз):

    sudo apt install -y xvfb x11vnc novnc websockify fonts-liberation fonts-noto-core
    venv/bin/playwright install --with-deps chromium firefox webkit
    # (+ playwright install chrome msedge — если у аккаунтов channel: chrome/msedge)

Запуск (из корня проекта, тем же venv, что и refresh-джобы; НЕ в контейнере):

    venv/bin/python3 scripts/manual_login.py --list
    venv/bin/python3 scripts/manual_login.py account_1

и открыть в браузере на домашнем ПК напечатанную ссылку
http://IP_СЕРВЕРА:6080/vnc.html?autoconnect=1&resize=scale
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Нет playwright в этом python (используй venv, где стоят refresh-джобы).")

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent))

from refresh_cookies import (  # noqa: E402
    atomic_write_json,
    cookie_editor_to_playwright,
    playwright_to_cookie_editor,
)
from scraper.config import load_accounts  # noqa: E402
from scraper.constants import BASE_DIR, PROXY_HOST, log  # noqa: E402

GUEST_CFG_PATH = _SCRIPTS_DIR / "refresh_guest_cookies.yaml"
LOGIN_CFG_PATH = _SCRIPTS_DIR / "refresh_cookies.yaml"
GEO_FALLBACK = {"locale": "en-US", "timezone_id": "America/New_York"}
NOVNC_DIRS = ("/usr/share/novnc", "/opt/novnc", "/usr/share/javascript/novnc")


# ---------------------------------------------------------------- #
#  Виртуальный экран + VNC + noVNC
# ---------------------------------------------------------------- #

class RemoteDesktop:
    def __init__(self, width: int, height: int, novnc_port: int, vnc_password: str | None):
        self.width, self.height = width, height
        self.novnc_port = novnc_port
        self.vnc_password = vnc_password
        self.display = ""
        self.procs: list[subprocess.Popen] = []

    def start(self) -> None:
        missing = [b for b in ("Xvfb", "x11vnc", "websockify") if not shutil.which(b)]
        if missing:
            sys.exit(f"Не хватает: {', '.join(missing)}\n"
                     "sudo apt install -y xvfb x11vnc novnc websockify")
        novnc_dir = next((d for d in NOVNC_DIRS if Path(d).is_dir()), None)
        if not novnc_dir:
            sys.exit("Не нашёл каталог noVNC (sudo apt install -y novnc)")

        n = next((i for i in range(99, 130) if not Path(f"/tmp/.X{i}-lock").exists()), None)
        if n is None:
            sys.exit("Не нашёл свободный номер X-дисплея (99..129)")
        self.display = f":{n}"
        vnc_port = 5900 + n

        self._spawn(["Xvfb", self.display, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp"])
        sock = Path(f"/tmp/.X11-unix/X{n}")
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.2)
        else:
            self.stop()
            sys.exit("Xvfb не поднялся")

        vnc_cmd = ["x11vnc", "-display", self.display, "-rfbport", str(vnc_port),
                   "-localhost", "-forever", "-shared", "-quiet"]
        vnc_cmd += ["-passwd", self.vnc_password] if self.vnc_password else ["-nopw"]
        self._spawn(vnc_cmd)
        self._spawn(["websockify", "--web", novnc_dir, str(self.novnc_port), f"127.0.0.1:{vnc_port}"])

        time.sleep(1.5)
        for p in self.procs:
            if p.poll() is not None:
                self.stop()
                sys.exit(f"Процесс {p.args[0]} сразу завершился (порт {self.novnc_port} занят?)")

    def _spawn(self, cmd: list[str]) -> None:
        self.procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        for p in reversed(self.procs):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs.clear()


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "IP_СЕРВЕРА"
    finally:
        s.close()


# ---------------------------------------------------------------- #
#  Профиль / проверки
# ---------------------------------------------------------------- #

def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def infer_profile(impersonate: str) -> dict:
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


def resolve_geo(name: str, guest_cfg: dict, login_cfg: dict) -> dict:
    for cfg in (login_cfg, guest_cfg):
        g = (cfg.get("account_geo") or {}).get(name)
        if g:
            return {**GEO_FALLBACK, **g}
    return dict(GEO_FALLBACK)


def exit_ip(proxy_url: str) -> str:
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        return opener.open("https://api.ipify.org", timeout=15).read().decode().strip()
    except Exception as e:
        return f"не удалось определить ({e})"


def is_logged_in(page, base_url: str) -> tuple[bool, str]:
    try:
        resp = page.request.get(base_url.rstrip("/") + "/api/me.json", timeout=15000)
        if resp.status != 200:
            return False, f"status {resp.status}"
        data = resp.json()
        d = data.get("data") if isinstance(data, dict) else None
        if isinstance(d, dict) and d.get("name"):
            return True, d["name"]
        return False, "нет data.name"
    except Exception as e:
        return False, str(e)


def verify_cookies(rows: list[dict], impersonate: str, proxy_url: str, base_url: str) -> tuple[bool, str]:
    if cffi_requests is None:
        return True, "curl_cffi не установлен — верификацию пропустил"
    cookies = {c["name"]: c["value"] for c in rows if c.get("name")}
    try:
        r = cffi_requests.get(
            f"{base_url.rstrip('/')}/r/AskReddit/comments.json", params={"limit": 5},
            cookies=cookies, impersonate=impersonate,
            proxies={"http": proxy_url, "https": proxy_url}, timeout=20,
        )
    except Exception as e:
        return False, f"запрос упал: {e}"
    return r.status_code == 200, f"comments.json -> {r.status_code}"


# ---------------------------------------------------------------- #
#  main
# ---------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("account", nargs="?", help="например account_1")
    ap.add_argument("--list", action="store_true", help="показать аккаунты и выйти")
    ap.add_argument("--novnc-port", type=int, default=6080)
    ap.add_argument("--vnc-password", default=os.environ.get("MANUAL_LOGIN_VNC_PASSWORD"),
                    help="пароль на VNC (по умолчанию без пароля — только для доверенной локалки)")
    ap.add_argument("--public-host", default=None, help="адрес сервера для ссылки (по умолчанию — LAN IP)")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--timeout-minutes", type=float, default=30)
    ap.add_argument("--post-login-wait", type=float, default=30,
                    help="сколько секунд ждать после обнаружения логина/регистрации, "
                         "чтобы успеть закончить онбординг (по умолчанию 30)")
    ap.add_argument("--fresh", action="store_true", help="чистая сессия, без сохранённых cookies/storage_state")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--force", action="store_true", help="писать cookies, даже если верификация не прошла")
    args = ap.parse_args()

    accounts = {a["name"]: a for a in load_accounts()}
    guest_cfg, login_cfg = load_yaml(GUEST_CFG_PATH), load_yaml(LOGIN_CFG_PATH)
    guests = guest_cfg.get("guest_accounts") or {}

    if args.list:
        print(f"{'аккаунт':<11} {'порт':<6} {'enabled':<8} {'тип':<6} impersonate")
        for n, a in accounts.items():
            print(f"{n:<11} {a.get('proxy_port'):<6} {str(a.get('enabled')):<8} "
                  f"{'guest' if n in guests else 'login':<6} {a.get('impersonate')}")
        return 0

    if not args.account or args.account not in accounts:
        sys.exit(f"Укажи аккаунт из accounts.yaml (см. --list). Получено: {args.account!r}")

    name = args.account
    acc = accounts[name]
    port, impersonate, user_agent = acc.get("proxy_port"), acc.get("impersonate"), acc.get("user_agent")
    if not (port and impersonate and user_agent):
        sys.exit(f"{name}: в accounts.yaml нужны proxy_port, impersonate и user_agent")

    if name in guests:
        log.warning("[%s] слот числится в guest_accounts — refresh_guest_cookies.py будет затирать "
                    "его cookies гостевыми. Убери его из guest_accounts, иначе логин потеряется.", name)

    g = guests.get(name) or {}
    prof = infer_profile(impersonate)
    engine = g.get("engine") or prof["engine"]
    channel = g["channel"] if "channel" in g else prof["channel"]
    device = g.get("device") or prof["device"]
    geo = resolve_geo(name, guest_cfg, login_cfg)
    base_url = login_cfg.get("reddit_base_url") or guest_cfg.get("reddit_base_url") or "https://www.reddit.com"

    proxy_url = f"http://{PROXY_HOST}:{port}"
    cookie_file = BASE_DIR / acc.get("cookie_file", f"cookies/{name}.json")
    state_path = BASE_DIR / "storage_state" / f"{name}.json"

    print(f"[{name}] прокси {proxy_url}, выходной IP: {exit_ip(proxy_url)}")
    print(f"[{name}] engine={engine} channel={channel} device={device} "
          f"locale={geo['locale']} tz={geo['timezone_id']}\n[{name}] UA: {user_agent}")

    storage_state: object = {"cookies": [], "origins": []}
    if not args.fresh:
        if state_path.exists():
            storage_state = str(state_path)
            print(f"[{name}] продолжаю сохранённую сессию: {state_path}")
        elif cookie_file.exists():
            try:
                import json
                raw = json.loads(cookie_file.read_text(encoding="utf-8"))
                storage_state = {"cookies": cookie_editor_to_playwright(raw), "origins": []}
                print(f"[{name}] стартую с текущих cookies ({len(raw)} шт.)")
            except (OSError, ValueError):
                pass

    desktop = RemoteDesktop(args.width, args.height, args.novnc_port, args.vnc_password)
    desktop.start()
    host = args.public_host or lan_ip()
    print(f"\n>>> Открой на домашнем ПК: http://{host}:{args.novnc_port}/vnc.html?autoconnect=1&resize=scale"
          + ("  (VNC-пароль задан)" if args.vnc_password else ""))
    os.environ["DISPLAY"] = desktop.display

    new_state = None
    logged_user = None
    deadline = time.monotonic() + args.timeout_minutes * 60
    try:
        with sync_playwright() as pw:
            launcher = getattr(pw, engine, None)
            if launcher is None:
                print(f"неизвестный engine {engine!r}")
                return 1

            ctx_kwargs: dict = {}
            if device:
                try:
                    ctx_kwargs = dict(pw.devices[device])
                except KeyError:
                    print(f"нет device-пресета {device!r} в этой версии Playwright")
                    return 1
                ctx_kwargs.pop("default_browser_type", None)
            else:
                ctx_kwargs["no_viewport"] = True
            ctx_kwargs["user_agent"] = user_agent

            launch_kwargs: dict = {"headless": False}
            if channel:
                launch_kwargs["channel"] = channel
            if engine == "chromium":
                launch_kwargs["args"] = [
                    "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                    f"--window-size={args.width},{args.height}", "--window-position=0,0",
                ]
            try:
                browser = launcher.launch(**launch_kwargs)
            except Exception as e:
                print(f"не смог запустить {engine}/{channel}: {e} (playwright install {channel or engine})")
                return 1

            try:
                context = browser.new_context(
                    storage_state=storage_state, proxy={"server": proxy_url},
                    locale=geo["locale"], timezone_id=geo["timezone_id"], **ctx_kwargs,
                )
                page = context.new_page()
                try:
                    page.goto(base_url.rstrip("/") + "/login", timeout=45000, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"[!] страница логина не открылась сразу ({e}) — открой reddit.com/login в окне сам")

                print(f"[{name}] Жду логин до {args.timeout_minutes:.0f} мин (окно браузера не закрывай)...")
                last_note = 0.0
                while time.monotonic() < deadline:
                    try:
                        page.wait_for_timeout(3000)
                    except Exception:
                        print(f"[{name}] браузер закрыт до завершения логина — ничего не сохранено")
                        return 1
                    ok, info = is_logged_in(page, base_url)
                    if ok:
                        logged_user = info
                        break
                    if time.monotonic() - last_note > 30:
                        last_note = time.monotonic()
                        print(f"[{name}] ещё не залогинен ({info})")

                if not logged_user:
                    print(f"[{name}] таймаут: логин не обнаружен")
                    return 1

                wait_s = max(0.0, args.post_login_wait)
                print(f"[{name}] залогинен как u/{logged_user}. Жду ещё {wait_s:.0f}с — "
                      "закончи регистрацию/онбординг, окно не закрывай...")
                # Периодически снимаем storage_state: если окно закроют раньше
                # времени, используем последний снимок, а не теряем сессию.
                last_snapshot = None
                wait_end = time.monotonic() + wait_s
                next_note = 0.0
                while True:
                    left = wait_end - time.monotonic()
                    if left <= 0:
                        break
                    if left <= next_note or next_note == 0.0:
                        print(f"[{name}] осталось ~{left:.0f}с")
                        next_note = left - 10
                    try:
                        page.wait_for_timeout(min(3000, max(100, int(left * 1000))))
                        last_snapshot = context.storage_state()
                    except Exception:
                        print(f"[{name}] окно закрыто во время ожидания — беру последний снимок сессии")
                        break
                try:
                    actual_ua = page.evaluate("navigator.userAgent")
                    if actual_ua != user_agent:
                        print(f"[!] фактический UA отличается от accounts.yaml: {actual_ua}")
                except Exception:
                    pass
                try:
                    new_state = context.storage_state()
                except Exception:
                    new_state = last_snapshot
                if new_state is None:
                    print(f"[{name}] не удалось получить сессию")
                    return 1
            finally:
                browser.close()
    finally:
        desktop.stop()

    reddit_cookies = [c for c in new_state.get("cookies", []) if "reddit.com" in (c.get("domain") or "")]
    if not reddit_cookies:
        print("ОШИБКА: нет ни одной cookie домена reddit.com")
        return 1
    rows = playwright_to_cookie_editor(reddit_cookies)

    verified = True
    if not args.no_verify:
        verified, info = verify_cookies(rows, impersonate, proxy_url, base_url)
        print(f"[{name}] верификация: {'OK' if verified else 'ПРОВАЛ'} ({info})")

    if not verified and not args.force:
        side = state_path.with_name(f"{name}.unverified.json")
        atomic_write_json(side, new_state)
        print(f"Рабочие cookies НЕ тронуты. Сессия сохранена в {side} (--force — записать всё равно).")
        return 2

    atomic_write_json(state_path, new_state)
    atomic_write_json(cookie_file, rows)
    print(f"[{name}] записано: {cookie_file} ({len(rows)} cookies), {state_path}")
    if not acc.get("enabled"):
        print(f"[!] {name}: enabled: false в accounts.yaml — включи и `docker compose restart scraper`.")
    else:
        print("Воркер подхватит cookies сам (hot-reload, до ~30с). Если он уже остановился из-за "
              "401/403 — нужен `docker compose restart scraper`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
