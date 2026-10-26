#!/usr/bin/env python3
"""
scripts/login_hub.py — «хаб» одновременного ручного входа в несколько аккаунтов
(модуль 02). Запускается НА СЕРВЕРЕ, поверх тех же приёмов, что и manual_login.py.

На каждый аккаунт — своя сессия: свой виртуальный экран (Xvfb :N), свой x11vnc
(127.0.0.1:5900+N), свой websockify/noVNC и свой видимый браузер с профилем
аккаунта через ТОТ ЖЕ прокси-порт mihomo, что использует скрапер (тот же выходной
IP). Ссылки печатаются таблицей: открываешь на рабочем ПК и логинишься руками.

    venv/bin/python3 scripts/login_hub.py --list-queue
    venv/bin/python3 scripts/login_hub.py --accounts account_11 account_12 account_13 account_14
    venv/bin/python3 scripts/login_hub.py --next 4            # 4 первых из очереди «нужен вход»
    venv/bin/python3 scripts/login_hub.py --next 4 --queue    # + подхватывать следующих по мере освобождения

Порты noVNC: login_hub.port_base + индекс (6001, 6002, ...). Освободившийся порт
(вход состоялся / таймаут / ошибка) переиспользуется следующим аккаунтом из очереди.

Как только /api/me.json вернёт data.name, сессия (как в manual_login.py): снимает
storage_state, проверяет cookies реальным запросом (curl_cffi, тот же impersonate и
прокси), атомарно пишет cookies/ и storage_state/ и закрывает ТОЛЬКО свою сессию —
остальные продолжают работать.

Лимит параллельных сессий: min(login_hub.max_parallel, свободная RAM // ram_per_session_mb).

Безопасность:
  * noVNC доступен только с --bind (LAN-адрес) и только клиентам из login_hub.allowed_cidr
    (фильтр — наш TCP-прокси перед websockify, root/iptables не нужны); сам websockify и
    x11vnc слушают только 127.0.0.1;
  * пароль VNC обязателен: генерируется на запуск и печатается (классический VNC-пароль —
    8 символов; хранится только в файле 0600 во временном каталоге, не в argv);
  * пароль от Reddit скрипт не видит, не хранит и не логирует: ты вводишь его прямо в
    браузере сессии; тело страниц и cookies в вывод не попадают.

Капча / подтверждение почты: сессия остаётся открытой до таймаута, статус waiting_user.
Статусы всех сессий пишутся в state/login_hub/status.json (для панели; пароль VNC — нет).

Зависимости — те же, что у manual_login.py (xvfb x11vnc novnc websockify + playwright).
"""

from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import os
import secrets
import select
import shutil
import signal
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import yaml

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent))

import account_creds as ac  # noqa: E402  (ник/пароль для --register)
import registration_hook as rh  # noqa: E402  (твоя логика авторегистрации)
import human_session as hs  # noqa: E402  (модуль 04)
import login_fleet as lf  # noqa: E402
import manual_login as ml  # noqa: E402  (is_logged_in, verify_cookies, exit_ip, infer_profile, resolve_geo...)
from refresh_cookies import atomic_write_json, playwright_to_cookie_editor  # noqa: E402
from scraper.config import ACCOUNTS_PATH, load_accounts  # noqa: E402
from scraper.constants import BASE_DIR, CONFIG_PATH, PROXY_HOST  # noqa: E402
from scraper.identity import STATE_DIR, write_json_atomic  # noqa: E402

HUB_DEFAULTS = {
    "port_base": 6001,            # первый порт noVNC; сессия N-я -> port_base + N
    "max_parallel": 4,            # максимум одновременных сессий
    "ram_per_session_mb": 700,    # сколько RAM «стоит» одна сессия (Xvfb + браузер)
    "allowed_cidr": "192.168.0.0/16",   # кому разрешён доступ к noVNC (через запятую — несколько)
    "bind": "192.168.0.101",      # LAN-интерфейс, на котором слушает noVNC
    "public_host": None,          # адрес в ссылках (по умолчанию = bind)
    "screen": [1400, 900],        # разрешение экрана, если в профиле его нет
}
PORT_RANGE_SLACK = 16             # запас портов на случай, если часть из диапазона занята чужим процессом
DISPLAY_RANGE = range(100, 200)
NOVNC_DIRS = ml.NOVNC_DIRS
VNC_PASSWORD_LEN = 8              # классический VNC-пароль: значимы только 8 символов

# статусы сессии
STARTING, WAITING_LOGIN, WAITING_USER, VERIFYING = "starting", "waiting_login", "waiting_user", "verifying"
DONE, FAILED, TIMEOUT, CANCELLED = "done", "failed", "timeout", "cancelled"
FINAL_STATUSES = (DONE, FAILED, TIMEOUT, CANCELLED)

ACCOUNTS_LOCK = threading.Lock()  # сессии параллельно правят accounts.yaml
PRINT_LOCK = threading.Lock()


def say(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


# ---------------------------------------------------------------- #
#  Конфиг, ресурсы, очередь
# ---------------------------------------------------------------- #

def load_hub_cfg(config_path: Path = CONFIG_PATH) -> dict:
    try:
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    return {**HUB_DEFAULTS, **(data.get("login_hub") or {})}


def free_ram_mb() -> int:
    """MemAvailable из /proc/meminfo (сервер — Linux)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def capacity(running: int, max_parallel: int, free_mb: int, per_session_mb: int) -> int:
    """Сколько сессий всего может быть одновременно: min(max_parallel, running + free//per).
    При running=0 это ровно min(max_parallel, свободная RAM // per_session_mb)."""
    per = max(1, int(per_session_mb))
    return max(0, min(int(max_parallel), running + max(0, int(free_mb)) // per))


def parse_cidrs(spec: str | list[str]) -> list[ipaddress._BaseNetwork]:
    parts = spec if isinstance(spec, list) else str(spec).split(",")
    nets = [ipaddress.ip_network(p.strip(), strict=False) for p in parts if str(p).strip()]
    if not nets:
        raise ValueError("пустой allowed_cidr")
    return nets


def client_allowed(ip: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in n for n in networks if n.version == addr.version)


def build_queue(accounts: list[dict], guests: dict, state_dir: Path = STATE_DIR,
                skip: set[str] | None = None) -> list[tuple[str, str]]:
    """Очередь «нужен вход»: [(account, статус)], сначала «нужен вход», затем «проверить вход»
    (внутри — по номеру). «Нужна нода» не берём: без ноды войти нельзя."""
    skip = skip or set()
    rank = {lf.NEED_LOGIN: 0, lf.CHECK_LOGIN: 1}
    rows = []
    for a in accounts:
        n = a["name"]
        if n in skip:
            continue
        status = lf.login_status({**a, "login": a.get("login") or n not in guests}, lf.slot_flags(state_dir, n))
        if status in rank:
            rows.append((rank[status], lf.acc_num(n), n, status))
    return [(n, s) for _, _, n, s in sorted(rows)]


# ---------------------------------------------------------------- #
#  Пулы портов / дисплеев
# ---------------------------------------------------------------- #

def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


class PortPool:
    """Порты noVNC: самый нижний свободный в [base, base + size). Для первой партии
    это ровно base + индекс; освободившийся порт берёт следующий аккаунт."""

    def __init__(self, base: int, size: int, is_free: Callable[[int], bool] = port_is_free):
        self.base, self.size, self._is_free = base, size, is_free
        self._used: dict[int, str] = {}
        self._lock = threading.Lock()

    def acquire(self, account: str) -> int | None:
        with self._lock:
            for p in range(self.base, self.base + self.size):
                if p not in self._used and self._is_free(p):
                    self._used[p] = account
                    return p
        return None

    def release(self, port: int) -> None:
        with self._lock:
            self._used.pop(port, None)

    def snapshot(self) -> dict[int, str]:
        with self._lock:
            return dict(self._used)


class DisplayPool:
    """Номера X-дисплеев: не занятые чужим Xvfb (/tmp/.X{n}-lock) и не выданные нами."""

    def __init__(self, numbers=DISPLAY_RANGE, tmp_dir: str = "/tmp"):
        self.numbers, self.tmp = list(numbers), Path(tmp_dir)
        self._used: set[int] = set()
        self._lock = threading.Lock()

    def acquire(self) -> int | None:
        with self._lock:
            for n in self.numbers:
                if n in self._used or (self.tmp / f".X{n}-lock").exists() or (self.tmp / ".X11-unix" / f"X{n}").exists():
                    continue
                if not port_is_free(5900 + n):
                    continue
                self._used.add(n)
                return n
        return None

    def release(self, n: int) -> None:
        with self._lock:
            self._used.discard(n)


# ---------------------------------------------------------------- #
#  CIDR-фильтр перед websockify
# ---------------------------------------------------------------- #

class CidrProxy:
    """TCP-прокси bind:port -> 127.0.0.1:target. Принимает только клиентов из allowed
    сетей (остальных закрывает сразу). Работает и для страниц noVNC, и для WebSocket."""

    def __init__(self, bind: str, port: int, target_port: int, networks, label: str = ""):
        self.bind, self.port, self.target_port = bind, port, target_port
        self.networks, self.label = networks, label
        self.rejected = 0
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._conns: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind, self.port))
        s.listen(32)
        s.settimeout(0.5)
        self._sock = s
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name=f"cidr-proxy-{self.port}")
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not client_allowed(addr[0], self.networks):
                self.rejected += 1
                conn.close()
                continue
            threading.Thread(target=self._pipe, args=(conn,), daemon=True).start()

    def _pipe(self, client: socket.socket) -> None:
        try:
            up = socket.create_connection(("127.0.0.1", self.target_port), timeout=5)
        except OSError:
            client.close()
            return
        with self._lock:
            self._conns.update((client, up))
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([client, up], [], [], 1.0)
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (up if s is client else client).sendall(data)
        except OSError:
            pass
        finally:
            with self._lock:
                self._conns.difference_update((client, up))
            for s in (client, up):
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)


# ---------------------------------------------------------------- #
#  Процессы: запуск и гарантированная зачистка
# ---------------------------------------------------------------- #

def spawn_process(cmd: list[str], log_path: Path | None = None) -> subprocess.Popen:
    """Свой process group (start_new_session) — чтобы гасить всё дерево разом."""
    out = open(log_path, "wb") if log_path else subprocess.DEVNULL
    try:
        return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        if log_path:
            out.close()


def terminate_process(p: subprocess.Popen, grace: float = 5.0) -> None:
    if p.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass
        try:
            p.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def kill_display_clients(display: str, proc_root: str = "/proc") -> int:
    """Добивает всё, что запущено с DISPLAY=<display> (хромиум и его дети, если
    Playwright не успел/не смог их закрыть). Возвращает число убитых процессов."""
    killed = 0
    me = os.getpid()
    want = f"DISPLAY={display}".encode()
    for d in Path(proc_root).iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            env = (d / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if want in env:
            try:
                os.kill(int(d.name), signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


class DesktopError(RuntimeError):
    pass


class Desktop:
    """Xvfb + x11vnc (только 127.0.0.1) + websockify (только 127.0.0.1) + CIDR-прокси на LAN."""

    def __init__(self, *, width: int, height: int, port: int, bind: str, networks, vnc_passwd_file: Path,
                 novnc_dir: str, displays: DisplayPool, log_dir: Path,
                 spawn: Callable[..., subprocess.Popen] = spawn_process, name: str = "",
                 x11_dir: str = "/tmp/.X11-unix", lock_dir: str = "/tmp"):
        self.width, self.height, self.port, self.bind = width, height, port, bind
        self.networks, self.passwd_file, self.novnc_dir = networks, vnc_passwd_file, novnc_dir
        self.displays, self.log_dir, self._spawn, self.name = displays, log_dir, spawn, name
        self.display = ""
        self._num: int | None = None
        self.procs: list[subprocess.Popen] = []
        self.proxy: CidrProxy | None = None
        self.x11_dir, self.lock_dir = Path(x11_dir), Path(lock_dir)
        self._stopped = False

    def start(self) -> None:
        n = self.displays.acquire()
        if n is None:
            raise DesktopError("нет свободного номера X-дисплея")
        self._num = n
        self.display = f":{n}"
        vnc_port = 5900 + n
        try:
            self._start_processes(n, vnc_port)
        except BaseException:
            self.stop()
            raise

    def _start_processes(self, n: int, vnc_port: int) -> None:
        tag = self.name or str(self.port)
        self._launch(["Xvfb", self.display, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp"],
                     f"{tag}-xvfb")
        sock = self.x11_dir / f"X{n}"
        for _ in range(50):
            if sock.exists():
                break
            if self.procs[0].poll() is not None:
                raise DesktopError(f"Xvfb {self.display} завершился: {self._tail(f'{tag}-xvfb')}")
            time.sleep(0.2)
        else:
            raise DesktopError("Xvfb не поднялся")

        self._launch(["x11vnc", "-display", self.display, "-rfbport", str(vnc_port), "-localhost",
                      "-forever", "-shared", "-quiet", "-passwdfile", str(self.passwd_file)], f"{tag}-x11vnc")
        ws_port = _free_local_port()
        self._launch(["websockify", "--web", self.novnc_dir, f"127.0.0.1:{ws_port}", f"127.0.0.1:{vnc_port}"],
                     f"{tag}-websockify")
        time.sleep(1.0)
        for p, nm in zip(self.procs[1:], ("x11vnc", "websockify")):
            if p.poll() is not None:
                raise DesktopError(f"{nm} сразу завершился: {self._tail(f'{tag}-{nm}')}")
        self.proxy = CidrProxy(self.bind, self.port, ws_port, self.networks, label=tag)
        try:
            self.proxy.start()
        except OSError as e:
            raise DesktopError(f"не смог занять {self.bind}:{self.port}: {e}") from e

    def _launch(self, cmd: list[str], logname: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.procs.append(self._spawn(cmd, self.log_dir / f"{logname}.log"))

    def _tail(self, logname: str) -> str:
        try:
            return (self.log_dir / f"{logname}.log").read_text(errors="replace").strip()[-300:] or "(пусто)"
        except OSError:
            return "(лог недоступен)"

    def stop(self) -> None:
        """Идемпотентно: прокси, процессы (с добиванием детей по DISPLAY), номер дисплея."""
        if self._stopped:
            return
        self._stopped = True
        if self.proxy:
            self.proxy.stop()
        for p in reversed(self.procs):
            terminate_process(p)
        if self.display:
            kill_display_clients(self.display)
        self.procs.clear()
        if self._num is not None:
            for f in (self.lock_dir / f".X{self._num}-lock", self.x11_dir / f"X{self._num}"):
                try:
                    f.unlink()
                except OSError:
                    pass
            self.displays.release(self._num)


def _free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- #
#  Профиль сессии (модуль 03, если профиль уже создан; иначе accounts.yaml)
# ---------------------------------------------------------------- #

def _int_between(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def resolve_profile(name: str, acc: dict, guests: dict, login_cfg: dict, guest_cfg: dict,
                    default_screen=(1400, 900), state_dir: Path = STATE_DIR) -> dict:
    """Профиль для браузера входа. Базу берём из accounts.yaml (как manual_login.py), а если есть
    state/profiles/<name>.json (модуль 03) — его поля (UA, locale, timezone, экран, железо) главнее."""
    g = guests.get(name) or {}
    inferred = ml.infer_profile(acc.get("impersonate"))
    geo = ml.resolve_geo(name, guest_cfg, login_cfg)
    prof = {
        "engine": g.get("engine") or inferred["engine"],
        "channel": g["channel"] if "channel" in g else inferred["channel"],
        "device": g.get("device") or inferred["device"],
        "user_agent": acc.get("user_agent"),
        "impersonate": acc.get("impersonate"),
        "locale": geo["locale"],
        "timezone_id": geo["timezone_id"],
        "screen": {"w": default_screen[0], "h": default_screen[1]},
        "device_pixel_ratio": None, "hardware_concurrency": None, "device_memory_gb": None,
        "webgl_vendor": None, "webgl_renderer": None, "languages": None, "accept_language": None,
        "mobile": False, "viewport": None,
        "source": "accounts.yaml",
    }
    try:
        pf = json.loads((state_dir / "profiles" / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pf = None
    if isinstance(pf, dict):
        for k in ("engine", "channel", "user_agent", "impersonate", "locale", "timezone_id",
                  "device_pixel_ratio", "hardware_concurrency", "device_memory_gb",
                  "webgl_vendor", "webgl_renderer", "languages", "accept_language", "mobile", "viewport"):
            if pf.get(k) is not None:
                prof[k] = pf[k]
        if "device" in pf:                      # None у десктопа: не подставлять мобильный пресет
            prof["device"] = pf["device"]
        scr = pf.get("screen")
        if isinstance(scr, dict) and scr.get("w") and scr.get("h"):
            prof["screen"] = {"w": scr["w"], "h": scr["h"]}
        prof["source"] = f"state/profiles/{name}.json"
    prof["screen"] = {"w": _int_between(prof["screen"]["w"], 800, 3840, 1400),
                      "h": _int_between(prof["screen"]["h"], 600, 2160, 900)}
    return prof


def hardware_init_script(prof: dict) -> str | None:
    parts = []
    if prof.get("hardware_concurrency"):
        parts.append(f"Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{int(prof['hardware_concurrency'])}}});")
    if prof.get("device_memory_gb"):
        parts.append(f"Object.defineProperty(navigator,'deviceMemory',{{get:()=>{float(prof['device_memory_gb'])}}});")
    if prof.get("webgl_vendor") and prof.get("webgl_renderer"):
        v, r = json.dumps(prof["webgl_vendor"]), json.dumps(prof["webgl_renderer"])
        parts.append(
            "for (const C of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) { if (!C) continue;"
            "const g = C.prototype.getParameter;"
            f"C.prototype.getParameter = function(p) {{ if (p === 37445) return {v}; if (p === 37446) return {r}; "
            "return g.call(this, p); }; }")
    if prof.get("languages"):
        parts.append(f"Object.defineProperty(navigator,'languages',{{get:()=>Object.freeze({json.dumps(prof['languages'])})}});")
    return "\n".join(parts) or None


# ---------------------------------------------------------------- #
#  Определение «нужно участие человека» (капча / письмо)
# ---------------------------------------------------------------- #

CAPTCHA_HINTS = ("captcha", "recaptcha", "hcaptcha", "turnstile", "challenge")
EMAIL_HINTS = ("verify your email", "check your email", "confirm your email", "email verification",
               "verification code", "подтвердите", "проверьте почту")


def detect_waiting_user(url: str, frame_urls: list[str], body_text: str) -> str | None:
    """Возвращает причину ('captcha' / 'email') или None. Текст страницы только анализируется,
    нигде не логируется и не сохраняется."""
    low_urls = [(url or "").lower()] + [(u or "").lower() for u in frame_urls]
    if any(h in u for u in low_urls for h in CAPTCHA_HINTS):
        return "captcha"
    text = (body_text or "").lower()
    if any(h in text for h in EMAIL_HINTS):
        return "email"
    return None


# ---------------------------------------------------------------- #
#  Сессия входа
# ---------------------------------------------------------------- #

class Cancelled(Exception):
    pass


class _Finished(Exception):
    """Выход из _browse при таймауте (статус TIMEOUT уже выставлен)."""


class Options:
    def __init__(self, timeout_minutes=30.0, post_login_wait=0.0, fresh=False, verify=True, force=False,
                 register=False, new_creds=False):
        self.timeout_minutes, self.post_login_wait = timeout_minutes, post_login_wait
        self.fresh, self.verify, self.force = fresh, verify, force
        self.register, self.new_creds = register, new_creds


class Context:
    """Общее для всех сессий хаба."""

    def __init__(self, *, cfg: dict, bind: str, networks, vnc_password: str, passwd_file: Path, novnc_dir: str,
                 ports: PortPool, displays: DisplayPool, log_dir: Path, options: Options,
                 cancel: threading.Event, accounts: dict, guests: dict, guest_cfg: dict, login_cfg: dict):
        self.cfg, self.bind, self.networks = cfg, bind, networks
        self.vnc_password, self.passwd_file, self.novnc_dir = vnc_password, passwd_file, novnc_dir
        self.ports, self.displays, self.log_dir, self.options = ports, displays, log_dir, options
        self.cancel = cancel
        self.accounts, self.guests, self.guest_cfg, self.login_cfg = accounts, guests, guest_cfg, login_cfg

    @property
    def host(self) -> str:
        return self.cfg.get("public_host") or self.bind


def link_for(host: str, port: int) -> str:
    return f"http://{host}:{port}/vnc.html?autoconnect=1&resize=scale"


class LoginSession:
    def __init__(self, ctx: Context, account: str, port: int):
        self.ctx, self.account, self.port = ctx, account, port
        self.status, self.message = STARTING, ""
        self.exit_ip: str | None = None
        self.user: str | None = None
        self.human_note = ""
        self.creds: dict | None = None      # ник/пароль для --register (в status.json не попадают)
        self._hook: rh.HookRunner | None = None
        self.started = time.time()
        self.desktop: Desktop | None = None
        self.link = link_for(ctx.host, port)

    def set(self, status: str, message: str = "") -> None:
        if self.status != status or message:
            self.status, self.message = status, message

    @property
    def finished(self) -> bool:
        return self.status in FINAL_STATUSES

    def _check_cancel(self) -> None:
        if self.ctx.cancel.is_set():
            raise Cancelled()

    # --- точка входа потока ---
    def run(self) -> None:
        try:
            self._run()
        except _Finished:
            pass
        except Cancelled:
            self.set(CANCELLED, "остановлено хабом")
        except Exception as e:  # noqa: BLE001 — сессия не должна ронять хаб
            self.set(FAILED, f"{type(e).__name__}: {e}")
        finally:
            self.close()

    def close(self) -> None:
        if self.desktop:
            self.desktop.stop()

    # --- основной сценарий ---
    def _run(self) -> None:
        ctx, opt, name = self.ctx, self.ctx.options, self.account
        acc = ctx.accounts[name]
        port, impersonate, user_agent = acc.get("proxy_port"), acc.get("impersonate"), acc.get("user_agent")
        if not (port and impersonate and user_agent):
            raise RuntimeError("в accounts.yaml нужны proxy_port, impersonate и user_agent")
        prof = resolve_profile(name, acc, ctx.guests, ctx.login_cfg, ctx.guest_cfg, tuple(ctx.cfg["screen"]))
        impersonate = prof.get("impersonate") or impersonate
        proxy_url = f"http://{PROXY_HOST}:{port}"
        if opt.register:
            self.creds, reused = ac.prepare(name, proxy_url, impersonate, new=opt.new_creds)
            self._hook = rh.HookRunner(rh.RegCtx(name, self.creds["username"], self.creds["password"],
                                                 ctx.login_cfg.get("reddit_base_url") or "https://www.reddit.com",
                                                 proxy_url, impersonate, log=say))
            say(f"[{name}] регистрация: ник {self.creds['username']}   пароль {self.creds['password']}   "
                f"({'прежние данные' if reused else 'сгенерировано'}; credentials/{name}.json, 0600)")
        base_url = ctx.login_cfg.get("reddit_base_url") or ctx.guest_cfg.get("reddit_base_url") or "https://www.reddit.com"
        cookie_file = BASE_DIR / acc.get("cookie_file", f"cookies/{name}.json")
        state_path = BASE_DIR / "storage_state" / f"{name}.json"
        deadline = time.monotonic() + opt.timeout_minutes * 60

        storage_state: object = {"cookies": [], "origins": []}
        if not opt.fresh:
            if state_path.exists():
                storage_state = str(state_path)
            elif cookie_file.exists():
                try:
                    raw = json.loads(cookie_file.read_text(encoding="utf-8"))
                    storage_state = {"cookies": ml.cookie_editor_to_playwright(raw), "origins": []}
                except (OSError, ValueError):
                    pass

        screen = prof["screen"]
        self.desktop = Desktop(width=screen["w"], height=screen["h"], port=self.port, bind=ctx.bind,
                               networks=ctx.networks, vnc_passwd_file=ctx.passwd_file, novnc_dir=ctx.novnc_dir,
                               displays=ctx.displays, log_dir=ctx.log_dir, name=name)
        self.desktop.start()
        self._check_cancel()
        threading.Thread(target=self._resolve_exit_ip, args=(proxy_url,), daemon=True).start()

        new_state = self._browse(prof, proxy_url, base_url, storage_state, deadline)
        self._check_cancel()
        self.set(VERIFYING)
        self._save(name, acc, new_state, impersonate, proxy_url, base_url, cookie_file, state_path)

    def _resolve_exit_ip(self, proxy_url: str) -> None:
        self.exit_ip = ml.exit_ip(proxy_url)

    def _browse(self, prof, proxy_url, base_url, storage_state, deadline) -> dict:
        from playwright.sync_api import sync_playwright

        ctx, opt, name = self.ctx, self.ctx.options, self.account
        screen = prof["screen"]
        with sync_playwright() as pw:
            launcher = getattr(pw, prof["engine"], None)
            if launcher is None:
                raise RuntimeError(f"неизвестный engine {prof['engine']!r}")
            ctx_kwargs: dict = {}
            if prof["device"]:
                try:
                    ctx_kwargs = dict(pw.devices[prof["device"]])
                except KeyError:
                    raise RuntimeError(f"нет device-пресета {prof['device']!r} в этой версии Playwright")
                ctx_kwargs.pop("default_browser_type", None)
            elif prof.get("device_pixel_ratio"):
                ctx_kwargs.update(viewport={"width": screen["w"], "height": screen["h"] - 80},
                                  screen={"width": screen["w"], "height": screen["h"]},
                                  device_scale_factor=float(prof["device_pixel_ratio"]))
            else:
                ctx_kwargs["no_viewport"] = True
            ctx_kwargs["user_agent"] = prof["user_agent"]
            if prof["source"].startswith("state/profiles"):
                # профиль устройства (модуль 03): мобильные размеры/DPR и Accept-Language как у скрапера
                vp = prof.get("viewport")
                if prof["device"] and vp and prof.get("device_pixel_ratio"):
                    ctx_kwargs.update(viewport={"width": vp["w"], "height": vp["h"]},
                                      screen={"width": screen["w"], "height": screen["h"]},
                                      device_scale_factor=float(prof["device_pixel_ratio"]))
                if prof.get("accept_language"):
                    ctx_kwargs["extra_http_headers"] = {"Accept-Language": prof["accept_language"]}

            # DISPLAY — только в env ЭТОГО браузера (os.environ общий для всех потоков хаба)
            launch_kwargs: dict = {"headless": False, "env": {**os.environ, "DISPLAY": self.desktop.display}}
            if prof["channel"]:
                launch_kwargs["channel"] = prof["channel"]
            if prof["engine"] == "chromium":
                launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                                         f"--window-size={screen['w']},{screen['h']}", "--window-position=0,0"]
            try:
                browser = launcher.launch(**launch_kwargs)
            except Exception as e:
                raise RuntimeError(f"не смог запустить {prof['engine']}/{prof['channel']}: {e}")
            try:
                context = browser.new_context(storage_state=storage_state, proxy={"server": proxy_url},
                                              locale=prof["locale"], timezone_id=prof["timezone_id"], **ctx_kwargs)
                init = hardware_init_script(prof)
                if init:
                    context.add_init_script(init)
                page = context.new_page()
                try:
                    page.goto(base_url.rstrip("/") + ("/register" if self.creds else "/login"), timeout=45000,
                              wait_until="domcontentloaded")
                except Exception:
                    pass  # человек откроет reddit.com/login сам
                if self._hook:
                    self._hook.page_ready(page)

                self.set(WAITING_LOGIN, f"регистрация: ник {self.creds['username']}" if self.creds else "")
                tick = 0
                while True:
                    self._check_cancel()
                    if time.monotonic() >= deadline:
                        self.set(TIMEOUT, f"логин не обнаружен за {opt.timeout_minutes:.0f} мин")
                        raise _Finished()
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        raise RuntimeError("браузер закрыт до завершения входа — ничего не сохранено")
                    tick += 1
                    if tick % 3:
                        continue
                    if self._hook:
                        self._hook.tick(page)
                    ok, info = ml.is_logged_in(page, base_url)
                    if ok:
                        self.user = info
                        if self.creds:
                            ac.mark_logged_in(name, info)
                        break
                    self._update_waiting_user(page)

                self.set(WAITING_LOGIN, f"u/{self.user} — онбординг, окно не закрывай")
                last_snapshot = None
                wait_end = time.monotonic() + max(0.0, opt.post_login_wait)
                while time.monotonic() < wait_end:
                    self._check_cancel()
                    try:
                        page.wait_for_timeout(1000)
                        last_snapshot = ml.light_snapshot(context, last_snapshot)
                    except Exception:
                        break  # окно закрыли — берём последний снимок
                self._human_after_login(context, page, base_url, name)
                try:
                    new_state = context.storage_state()
                except Exception:
                    new_state = last_snapshot
                if new_state is None:
                    raise RuntimeError("не удалось получить сессию")
                return new_state
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    def _human_after_login(self, context, page, base_url: str, name: str) -> None:
        """Модуль 04: короткая «читающая» сессия сразу после первого входа (только чтение).
        Только если человек уже закончил онбординг (post_login_wait прошёл) и окно живо.
        Любая ошибка/капча не валит вход: сессия всё равно сохраняется, статус — в сообщении."""
        try:
            hcfg = hs.load_human_cfg()
            if not (hcfg.get("enabled") and hcfg.get("on_first_login")):
                return
            if self.ctx.cancel.is_set():
                return
            hcfg = {**hcfg, "max_session_seconds": min(hcfg["max_session_seconds"], 90.0),
                    "min_session_seconds": min(hcfg["min_session_seconds"], 40.0)}
            self.set(WAITING_LOGIN, f"u/{self.user} — «живая» сессия (только чтение), окно не закрывай")
            res = hs.maintain(name, context, page, hcfg, base_url=base_url, storage_state_path=None,
                              cookies=[], force=True)
            if res.status == hs.NEEDS_ATTENTION:
                self.human_note = f"; human: needs_attention ({res.detail})"
        except Exception as e:  # noqa: BLE001 — вход важнее имитации
            self.human_note = f"; human: пропущено ({type(e).__name__})"

    def _update_waiting_user(self, page) -> None:
        try:
            frames = [f.url for f in page.frames]
            try:
                body = page.inner_text("body", timeout=2000)[:5000]
            except Exception:
                body = ""
            reason = detect_waiting_user(page.url, frames, body)
        except Exception:
            return
        if reason:
            self.set(WAITING_USER, reason)
        elif self.status == WAITING_USER:
            self.set(WAITING_LOGIN)

    def _save(self, name, acc, new_state, impersonate, proxy_url, base_url, cookie_file, state_path) -> None:
        ctx, opt = self.ctx, self.ctx.options
        reddit_cookies = [c for c in new_state.get("cookies", []) if "reddit.com" in (c.get("domain") or "")]
        if not reddit_cookies:
            raise RuntimeError("нет ни одной cookie домена reddit.com")
        rows = playwright_to_cookie_editor(reddit_cookies)
        if opt.verify:
            verified, info = ml.verify_cookies(rows, impersonate, proxy_url, base_url)
            if not verified and not opt.force:
                side = state_path.with_name(f"{name}.unverified.json")
                atomic_write_json(side, new_state)
                raise RuntimeError(f"верификация провалена ({info}); рабочие cookies НЕ тронуты, "
                                   f"сессия в {side.name} (--force — записать всё равно)")
        atomic_write_json(state_path, new_state)
        atomic_write_json(cookie_file, rows)
        lf.clear_check_login(STATE_DIR, name)
        note = ""
        if not acc.get("enabled") and acc.get("login") and name not in ctx.guests:
            try:
                with ACCOUNTS_LOCK:
                    text = ACCOUNTS_PATH.read_text(encoding="utf-8")
                    with open(ACCOUNTS_PATH, "r+", encoding="utf-8") as f:  # тот же inode (файл смонтирован в контейнер)
                        f.seek(0)
                        f.write(lf.set_account_key(text, name, "enabled", "true"))
                        f.truncate()
                note = "; enabled: true записан (docker compose restart scraper)"
            except OSError as e:
                note = f"; НЕ смог включить слот в accounts.yaml ({e}) — поставь enabled: true руками"
        self.set(DONE, f"u/{self.user}, cookies: {len(rows)}{note}{self.human_note}")


# ---------------------------------------------------------------- #
#  Хаб
# ---------------------------------------------------------------- #

class Hub:
    def __init__(self, ctx: Context, pending: list[str], *, queue_source: Callable[[set[str]], list[str]] | None = None,
                 session_factory: Callable[[Context, str, int], object] = LoginSession,
                 free_ram: Callable[[], int] = free_ram_mb, status_path: Path | None = None,
                 poll_seconds: float = 1.0):
        self.ctx = ctx
        self.pending = collections.deque(pending)
        self.queue_source = queue_source
        self.factory, self.free_ram = session_factory, free_ram
        self.status_path = status_path
        self.poll = poll_seconds
        # лимит из ТЗ: min(max_parallel, свободная RAM // ram_per_session_mb) на момент старта хаба
        self.limit = capacity(0, ctx.cfg["max_parallel"], free_ram(), ctx.cfg["ram_per_session_mb"])
        self.attempted: set[str] = set()
        self.running: dict[str, tuple[object, threading.Thread]] = {}
        self.sessions: list[object] = []  # все когда-либо запущенные (для итогов)
        self._warned_ips: set[str] = set()

    # --- планирование ---
    def _capacity(self) -> int:
        # потолок — лимит старта; плюс живая проверка: новая сессия стартует, только если RAM ещё есть
        c = self.ctx.cfg
        return min(self.limit, capacity(len(self.running), c["max_parallel"], self.free_ram(), c["ram_per_session_mb"]))

    def _refill_pending(self) -> None:
        if not self.pending and self.queue_source:
            busy = self.attempted | set(self.running)
            for n in self.queue_source(busy):
                if n not in busy and n not in self.pending:
                    self.pending.append(n)

    def _start_ready(self) -> None:
        while not self.ctx.cancel.is_set():
            self._refill_pending()
            if not self.pending or len(self.running) >= self._capacity():
                return
            name = self.pending[0]
            port = self.ctx.ports.acquire(name)
            if port is None:
                return  # нет свободных портов — ждём освобождения
            self.pending.popleft()
            self.attempted.add(name)
            sess = self.factory(self.ctx, name, port)
            th = threading.Thread(target=self._worker, args=(sess,), daemon=True, name=f"login-{name}")
            th.start()
            self.running[name] = (sess, th)
            self.sessions.append(sess)
            say(f"[hub] {name}: порт {port} -> {sess.link}")

    def _worker(self, sess) -> None:
        try:
            sess.run()
        finally:
            self.ctx.ports.release(sess.port)  # порт свободен сразу после зачистки сессии

    def _reap(self) -> None:
        for name, (sess, th) in list(self.running.items()):
            if not th.is_alive():
                th.join()
                del self.running[name]
                extra = f" ({sess.message})" if sess.message else ""
                say(f"[hub] {name}: {sess.status}{extra}; порт {sess.port} свободен")

    def _check_duplicate_ips(self) -> None:
        seen: dict[str, str] = {}
        for name, (sess, _) in self.running.items():
            ip = getattr(sess, "exit_ip", None)
            if not ip or ip.startswith("не удалось"):
                continue
            if ip in seen and ip not in self._warned_ips:
                self._warned_ips.add(ip)
                say(f"[!] {seen[ip]} и {name} выходят с одного IP {ip} — у слотов должны быть РАЗНЫЕ ноды")
            seen[ip] = name

    def write_status(self) -> None:
        if not self.status_path:
            return
        rows = [{"account": s.account, "port": s.port, "link": s.link, "status": s.status, "message": s.message,
                 "exit_ip": getattr(s, "exit_ip", None), "started_at": int(getattr(s, "started", 0))}
                for s in self.sessions]
        try:
            write_json_atomic(self.status_path, {"updated_at": int(time.time()), "bind": self.ctx.bind,
                                                 "pending": list(self.pending), "sessions": rows})
        except OSError:
            pass

    # --- жизненный цикл ---
    def run(self) -> int:
        try:
            while True:
                self._reap()
                self._start_ready()
                self._check_duplicate_ips()
                self.write_status()
                if self.ctx.cancel.is_set():
                    break
                if not self.running and not self.pending:
                    self._refill_pending()
                    if not self.pending:
                        break
                if not self.running and self.pending and self._capacity() == 0:
                    say("[hub] не хватает RAM даже на одну сессию — выхожу")
                    break
                time.sleep(self.poll)
        finally:
            cancelled = self.ctx.cancel.is_set()   # до shutdown(): он сам ставит cancel
            self.shutdown()
        if cancelled:
            return 130
        return 0 if self.sessions and all(s.status == DONE for s in self.sessions) else 1

    def shutdown(self, join_timeout: float = 30.0) -> None:
        """Гасит всё: сигнал отмены, ожидание потоков, принудительная зачистка оставшегося."""
        self.ctx.cancel.set()
        end = time.monotonic() + join_timeout
        for sess, th in list(self.running.values()):
            th.join(timeout=max(0.1, end - time.monotonic()))
        for sess, th in list(self.running.values()):
            if th.is_alive():
                sess.close()  # браузер завис — рубим Xvfb/x11vnc/websockify и всё с его DISPLAY
        for sess, th in list(self.running.values()):
            th.join(timeout=5)
            self.ctx.ports.release(sess.port)
        self._reap_final()
        self.write_status()

    def _reap_final(self) -> None:
        for name, (sess, th) in list(self.running.items()):
            if not th.is_alive():
                self.running.pop(name, None)
                if not sess.finished:
                    sess.set(CANCELLED, "остановлено хабом")


# ---------------------------------------------------------------- #
#  CLI
# ---------------------------------------------------------------- #

def gen_vnc_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(VNC_PASSWORD_LEN))


def print_table(rows: list[tuple[str, int, str]]) -> None:
    w = max([len(r[0]) for r in rows] + [7])
    say(f"{'аккаунт':<{w}}  {'порт':<5}  ссылка")
    for name, port, link in rows:
        say(f"{name:<{w}}  {port:<5}  {link}")


def check_dependencies() -> str:
    missing = [b for b in ("Xvfb", "x11vnc", "websockify") if not shutil.which(b)]
    if missing:
        sys.exit(f"Не хватает: {', '.join(missing)}\nsudo apt install -y xvfb x11vnc novnc websockify")
    novnc = next((d for d in NOVNC_DIRS if Path(d).is_dir()), None)
    if not novnc:
        sys.exit("Не нашёл каталог noVNC (sudo apt install -y novnc)")
    return novnc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--accounts", nargs="+", metavar="ACCOUNT", help="например account_11 account_12 ...")
    ap.add_argument("--next", type=int, metavar="N", help="взять N первых из очереди «нужен вход»")
    ap.add_argument("--queue", action="store_true",
                    help="по мере освобождения портов брать следующих аккаунтов из очереди «нужен вход»")
    ap.add_argument("--list-queue", action="store_true", help="показать очередь «нужен вход» и выйти")
    ap.add_argument("--bind", help="LAN-адрес, на котором слушает noVNC (по умолчанию login_hub.bind)")
    ap.add_argument("--unsafe-bind-any", action="store_true", help="разрешить --bind 0.0.0.0 (не рекомендуется)")
    ap.add_argument("--public-host", help="адрес в ссылках (по умолчанию = bind)")
    ap.add_argument("--port-base", type=int, help="первый порт noVNC (по умолчанию login_hub.port_base)")
    ap.add_argument("--max-parallel", type=int, help="максимум одновременных сессий (login_hub.max_parallel)")
    ap.add_argument("--allowed-cidr", help="кому открыт noVNC, через запятую (login_hub.allowed_cidr)")
    ap.add_argument("--vnc-password", default=os.environ.get("LOGIN_HUB_VNC_PASSWORD"),
                    help="пароль VNC (по умолчанию генерируется и печатается); не короче 1 символа, значимы первые 8")
    ap.add_argument("--timeout-minutes", type=float, default=30)
    ap.add_argument("--post-login-wait", type=float, default=0,
                    help="сколько секунд ждать после входа (онбординг); по умолчанию 0: «живая» сессия идёт сразу")
    ap.add_argument("--fresh", action="store_true", help="чистая сессия, без сохранённых cookies/storage_state")
    ap.add_argument("--register", action="store_true",
                    help="регистрация нового аккаунта: придумать ник и пароль (credentials/<account>.json), открыть "
                         "/register и вызвать scripts/registration_hook.py (твоя логика заполнения формы)")
    ap.add_argument("--new-creds", action="store_true", help="с --register: сгенерировать данные заново")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--force", action="store_true", help="писать cookies, даже если верификация не прошла")
    args = ap.parse_args(argv)

    cfg = load_hub_cfg()
    for key, val in (("bind", args.bind), ("public_host", args.public_host), ("port_base", args.port_base),
                     ("max_parallel", args.max_parallel), ("allowed_cidr", args.allowed_cidr)):
        if val:
            cfg[key] = val

    accounts_list = load_accounts()
    accounts = {a["name"]: a for a in accounts_list}
    guest_cfg = ml.load_yaml(ml.GUEST_CFG_PATH)
    login_cfg = ml.load_yaml(ml.LOGIN_CFG_PATH)
    guests = guest_cfg.get("guest_accounts") or {}

    def queue_names(skip: set[str]) -> list[str]:
        return [n for n, _ in build_queue(accounts_list, guests, STATE_DIR, skip)]

    if args.list_queue:
        q = build_queue(accounts_list, guests)
        for n, s in q:
            say(f"{n:<11} {accounts[n].get('proxy_port'):<6} {s}")
        say(f"\nв очереди: {len(q)}")
        return 0

    if not (args.accounts or args.next or args.queue):
        ap.error("укажи --accounts ..., --next N или --queue")

    pending: list[str] = []
    for n in args.accounts or []:
        if n not in accounts:
            sys.exit(f"{n}: нет в accounts.yaml (см. --list-queue)")
        if n not in pending:
            pending.append(n)
    if args.next:
        pending += [n for n in queue_names(set(pending))[:args.next]]
    if not pending and not args.queue:
        sys.exit("очередь «нужен вход» пуста — входить не в кого")

    try:
        networks = parse_cidrs(cfg["allowed_cidr"])
        bind_ip = ipaddress.ip_address(cfg["bind"])
    except ValueError as e:
        sys.exit(f"login_hub: неверный allowed_cidr/bind: {e}")
    if bind_ip.is_unspecified and not args.unsafe_bind_any:
        sys.exit("--bind 0.0.0.0 открывает noVNC на все интерфейсы; укажи LAN-адрес или --unsafe-bind-any")
    with socket.socket() as probe:
        try:
            probe.bind((str(bind_ip), 0))
        except OSError as e:
            sys.exit(f"{bind_ip}: этот адрес не принадлежит серверу ({e}); задай --bind или login_hub.bind")

    novnc_dir = check_dependencies()
    free_mb = free_ram_mb()
    limit = capacity(0, cfg["max_parallel"], free_mb, cfg["ram_per_session_mb"])
    if limit < 1:
        sys.exit(f"Свободно {free_mb} МБ RAM — меньше {cfg['ram_per_session_mb']} МБ на одну сессию")

    password = args.vnc_password or gen_vnc_password()
    if not password:
        sys.exit("пароль VNC обязателен")

    tmp = Path(tempfile.mkdtemp(prefix="login-hub-"))
    os.chmod(tmp, 0o700)
    passwd_file = tmp / "vncpasswd"
    fd = os.open(passwd_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password + "\n")

    cancel = threading.Event()
    options = Options(args.timeout_minutes, args.post_login_wait, args.fresh, not args.no_verify, args.force,
                      args.register, args.new_creds)
    ctx = Context(cfg=cfg, bind=str(bind_ip), networks=networks, vnc_password=password, passwd_file=passwd_file,
                  novnc_dir=novnc_dir, ports=PortPool(cfg["port_base"], cfg["max_parallel"] + PORT_RANGE_SLACK),
                  displays=DisplayPool(), log_dir=tmp / "logs", options=options, cancel=cancel,
                  accounts=accounts, guests=guests, guest_cfg=guest_cfg, login_cfg=login_cfg)

    hub = Hub(ctx, pending, queue_source=queue_names if args.queue else None,
              status_path=STATE_DIR / "login_hub" / "status.json")

    def on_signal(signum, _frame):
        say(f"\n[hub] сигнал {signum} — гашу все сессии...")
        cancel.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    say(f"[hub] noVNC: {ctx.bind}:{cfg['port_base']}+ , доступ только из {cfg['allowed_cidr']}; "
        f"параллельно до {limit} (RAM свободно {free_mb} МБ)")
    say(f"[hub] ПАРОЛЬ VNC (один на все сессии этого запуска; noVNC спросит его): {password}")
    first = pending[:limit]
    print_table([(n, cfg["port_base"] + i, link_for(ctx.host, cfg["port_base"] + i)) for i, n in enumerate(first)])
    if len(pending) > limit:
        say(f"[hub] в очереди ещё: {', '.join(pending[limit:])} — стартуют по мере освобождения портов")

    try:
        rc = hub.run()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    say("\nИтог:")
    for s in hub.sessions:
        say(f"  {s.account:<11} порт {s.port}  {s.status}" + (f"  ({s.message})" if s.message else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())