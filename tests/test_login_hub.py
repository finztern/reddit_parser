"""Модуль 02 — login_hub.py: распределение портов, лимит по RAM, очередь, CIDR-фильтр,
зачистка процессов при отмене. Всё на заглушках: ни Xvfb, ни браузера, ни сети."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import login_hub as lh  # noqa: E402
import login_fleet as lf  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="loginhub-"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, timeout=8.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # зомби (ещё не reaped) считаем мёртвым
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


# ---------------------------------------------------------------- #
#  Лимит по RAM, порты, очередь
# ---------------------------------------------------------------- #

def test_capacity_is_min_of_max_parallel_and_ram():
    assert lh.capacity(0, 4, 100_000, 700) == 4
    assert lh.capacity(0, 4, 1500, 700) == 2          # 1500 // 700
    assert lh.capacity(0, 4, 699, 700) == 0
    assert lh.capacity(0, 4, 0, 700) == 0
    # уже работают 2 сессии, свободно ещё на одну -> всего 3
    assert lh.capacity(2, 4, 800, 700) == 3
    assert lh.capacity(3, 4, 100_000, 700) == 4


def test_port_pool_sequential_reuse_and_foreign_busy_port():
    pool = lh.PortPool(6001, 8, is_free=lambda p: True)
    got = [pool.acquire(f"account_{i}") for i in range(11, 15)]
    assert got == [6001, 6002, 6003, 6004]
    pool.release(6002)
    assert pool.acquire("account_15") == 6002          # освободившийся — первым
    # порт занят чужим процессом -> пропускаем
    busy = {6001, 6002}
    pool2 = lh.PortPool(6001, 4, is_free=lambda p: p not in busy)
    assert pool2.acquire("a") == 6003
    # диапазон кончился
    pool3 = lh.PortPool(6001, 2, is_free=lambda p: True)
    assert [pool3.acquire("a"), pool3.acquire("b"), pool3.acquire("c")] == [6001, 6002, None]


def test_build_queue_order_and_skips():
    state = _tmp()
    (state / "identities.json").write_text(json.dumps({"slots": {"account_13": {"needs_node": True}}}))
    lf.set_check_login(state, "account_14", "node replaced")
    accounts = [
        {"name": "account_1", "login": True, "enabled": True},       # ok
        {"name": "account_14", "login": True, "enabled": True},      # проверить вход
        {"name": "account_12", "login": True, "enabled": False},     # нужен вход
        {"name": "account_13", "login": True, "enabled": True},      # нужна нода -> не берём
        {"name": "account_11", "login": True, "enabled": False},     # нужен вход
        {"name": "account_20", "enabled": False},                    # гость (не логин) -> не берём
    ]
    q = lh.build_queue(accounts, {"account_20": {}}, state)
    assert q == [("account_11", lf.NEED_LOGIN), ("account_12", lf.NEED_LOGIN), ("account_14", lf.CHECK_LOGIN)]
    assert [n for n, _ in lh.build_queue(accounts, {"account_20": {}}, state, skip={"account_11"})][0] == "account_12"


def test_hub_config_defaults_and_repo_config():
    cfg = lh.load_hub_cfg(_tmp() / "missing.yaml")
    assert (cfg["port_base"], cfg["max_parallel"], cfg["ram_per_session_mb"]) == (6001, 4, 700)
    assert cfg["allowed_cidr"] == "192.168.0.0/16" and cfg["bind"] == "192.168.0.101"
    repo = lh.load_hub_cfg(ROOT / "config.yaml")
    assert repo["port_base"] == 6001 and repo["max_parallel"] == 4


def test_link_format():
    assert lh.link_for("192.168.0.101", 6001) == "http://192.168.0.101:6001/vnc.html?autoconnect=1&resize=scale"


def test_vnc_password_generated():
    a, b = lh.gen_vnc_password(), lh.gen_vnc_password()
    assert len(a) == lh.VNC_PASSWORD_LEN and a.isalnum() and a != b


# ---------------------------------------------------------------- #
#  Безопасность: CIDR-фильтр
# ---------------------------------------------------------------- #

def test_client_allowed():
    nets = lh.parse_cidrs("192.168.0.0/16, 10.1.0.0/24")
    assert lh.client_allowed("192.168.5.7", nets)
    assert lh.client_allowed("10.1.0.9", nets)
    assert not lh.client_allowed("10.2.0.9", nets)
    assert not lh.client_allowed("8.8.8.8", nets)
    assert lh.client_allowed("::ffff:192.168.0.5", nets)      # IPv4-mapped
    assert not lh.client_allowed("not-an-ip", nets)


def _echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            def h(c=c):
                try:
                    while (d := c.recv(1024)):
                        c.sendall(d)
                finally:
                    c.close()
            threading.Thread(target=h, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv


def test_cidr_proxy_passes_allowed_and_drops_others():
    echo = _echo_server()
    target = echo.getsockname()[1]
    ok_port, bad_port = _free_port(), _free_port()
    ok = lh.CidrProxy("127.0.0.1", ok_port, target, lh.parse_cidrs("127.0.0.0/8"))
    bad = lh.CidrProxy("127.0.0.1", bad_port, target, lh.parse_cidrs("10.0.0.0/8"))
    ok.start(); bad.start()
    try:
        with socket.create_connection(("127.0.0.1", ok_port), timeout=3) as c:
            c.sendall(b"ping")
            assert c.recv(16) == b"ping"
        with socket.create_connection(("127.0.0.1", bad_port), timeout=3) as c:
            c.settimeout(3)
            try:
                c.sendall(b"ping")
            except OSError:
                pass
            try:
                assert c.recv(16) == b""                  # закрыто без ответа
            except ConnectionResetError:
                pass
        assert _wait(lambda: bad.rejected == 1)
        assert ok.rejected == 0
    finally:
        ok.stop(); bad.stop(); echo.close()
    assert lh.port_is_free(ok_port) and lh.port_is_free(bad_port)


def test_main_refuses_bind_any_and_requires_a_target():
    for argv, code_is_str in ((["--accounts", "account_1", "--bind", "0.0.0.0"], True), ([], False)):
        try:
            lh.main(argv)
        except SystemExit as e:
            assert (isinstance(e.code, str) and "0.0.0.0" in e.code) if code_is_str else e.code == 2
        else:
            raise AssertionError("ожидали SystemExit")


# ---------------------------------------------------------------- #
#  Профиль, капча/почта
# ---------------------------------------------------------------- #

def test_resolve_profile_prefers_module03_profile_file():
    state = _tmp()
    acc = {"name": "account_11", "impersonate": "chrome131", "user_agent": "UA-from-accounts"}
    base = lh.resolve_profile("account_11", acc, {}, {}, {}, (1400, 900), state)
    assert base["source"] == "accounts.yaml" and base["screen"] == {"w": 1400, "h": 900}
    assert base["user_agent"] == "UA-from-accounts" and base["engine"] == "chromium"

    (state / "profiles").mkdir()
    (state / "profiles" / "account_11.json").write_text(json.dumps({
        "user_agent": "UA-from-profile", "locale": "de-DE", "timezone_id": "Europe/Berlin",
        "screen": {"w": 1920, "h": 1080}, "hardware_concurrency": 8, "device_memory_gb": 16,
        "webgl_vendor": "Google Inc. (NVIDIA)", "webgl_renderer": "ANGLE (NVIDIA)"}))
    p = lh.resolve_profile("account_11", acc, {}, {}, {}, (1400, 900), state)
    assert p["source"].endswith("account_11.json")
    assert (p["user_agent"], p["locale"], p["timezone_id"]) == ("UA-from-profile", "de-DE", "Europe/Berlin")
    assert p["screen"] == {"w": 1920, "h": 1080}          # экран Xvfb = разрешение профиля
    script = lh.hardware_init_script(p)
    assert "hardwareConcurrency" in script and "deviceMemory" in script and "37445" in script

    (state / "profiles" / "account_11.json").write_text(json.dumps({"screen": {"w": 99999, "h": 10}}))
    p = lh.resolve_profile("account_11", acc, {}, {}, {}, (1400, 900), state)
    assert p["screen"] == {"w": 3840, "h": 600}            # зажато в разумные пределы


def test_detect_waiting_user():
    assert lh.detect_waiting_user("https://www.reddit.com/login", [], "Log In") is None
    assert lh.detect_waiting_user("https://x", ["https://www.google.com/recaptcha/api2/anchor"], "") == "captcha"
    assert lh.detect_waiting_user("https://www.reddit.com/captcha", [], "") == "captcha"
    assert lh.detect_waiting_user("https://x", [], "Please Verify your email to continue") == "email"


# ---------------------------------------------------------------- #
#  Процессы: зачистка
# ---------------------------------------------------------------- #

def _fake_spawn_factory(x11_dir: Path, spawned: list):
    """Заглушка вместо Xvfb/x11vnc/websockify: `sleep`; для Xvfb создаёт X-сокет-файл."""
    def spawn(cmd, log_path=None):
        if cmd[0] == "Xvfb":
            (x11_dir / f"X{int(cmd[1][1:])}").touch()
        p = lh.spawn_process(["sleep", "300"])
        spawned.append(p)
        return p
    return spawn


def test_desktop_stop_kills_everything_and_frees_resources():
    tmp = _tmp()
    x11, locks = tmp / "x11", tmp / "locks"
    x11.mkdir(); locks.mkdir()
    spawned: list = []
    displays = lh.DisplayPool(range(150, 160), tmp_dir=str(locks))
    port = _free_port()
    d = lh.Desktop(width=1400, height=900, port=port, bind="127.0.0.1", networks=lh.parse_cidrs("127.0.0.0/8"),
                   vnc_passwd_file=tmp / "pw", novnc_dir="/nonexistent", displays=displays, log_dir=tmp / "logs",
                   spawn=_fake_spawn_factory(x11, spawned), name="account_11", x11_dir=str(x11), lock_dir=str(locks))
    d.start()
    assert len(spawned) == 3 and not lh.port_is_free(port)
    # «осиротевший» потомок: не наш Popen, но с DISPLAY этого экрана (как хромиум)
    child = subprocess.Popen(["sleep", "300"], env={**os.environ, "DISPLAY": d.display}, start_new_session=True)
    n = int(d.display[1:])
    assert n in displays._used
    d.stop()
    assert _wait(lambda: not any(_alive(p.pid) for p in spawned))
    child.wait(timeout=5)                                   # убит по DISPLAY
    assert lh.port_is_free(port)
    assert n not in displays._used and not (x11 / f"X{n}").exists()
    d.stop()                                                # идемпотентно


def test_desktop_start_failure_cleans_up():
    tmp = _tmp()
    x11, locks = tmp / "x11", tmp / "locks"
    x11.mkdir(); locks.mkdir()
    spawned: list = []

    def spawn(cmd, log_path=None):
        if cmd[0] == "Xvfb":
            (x11 / f"X{int(cmd[1][1:])}").touch()
        p = lh.spawn_process(["sleep", "300"] if cmd[0] != "x11vnc" else ["false"])   # x11vnc «падает»
        spawned.append(p)
        return p

    displays = lh.DisplayPool(range(160, 170), tmp_dir=str(locks))
    d = lh.Desktop(width=800, height=600, port=_free_port(), bind="127.0.0.1", networks=lh.parse_cidrs("127.0.0.0/8"),
                   vnc_passwd_file=tmp / "pw", novnc_dir="/x", displays=displays, log_dir=tmp / "logs",
                   spawn=spawn, x11_dir=str(x11), lock_dir=str(locks))
    try:
        d.start()
    except lh.DesktopError as e:
        assert "x11vnc" in str(e)
    else:
        raise AssertionError("ожидали DesktopError")
    assert _wait(lambda: not any(_alive(p.pid) for p in spawned))
    assert not displays._used


# ---------------------------------------------------------------- #
#  Хаб на заглушках сессий
# ---------------------------------------------------------------- #

class FakeSession:
    """Сессия-заглушка. behaviour: 'done' — сразу успех; 'block' — ждёт отмены."""
    instances: list = []

    def __init__(self, ctx, account, port, behaviour="block"):
        self.ctx, self.account, self.port, self.behaviour = ctx, account, port, behaviour
        self.status, self.message, self.exit_ip, self.started = lh.STARTING, "", None, time.time()
        self.link = lh.link_for(ctx.host, port)
        self.closed = False
        FakeSession.instances.append(self)

    @property
    def finished(self):
        return self.status in lh.FINAL_STATUSES

    def set(self, status, message=""):
        self.status, self.message = status, message

    def run(self):
        try:
            self.set(lh.WAITING_LOGIN)
            if self.behaviour == "done":
                time.sleep(0.2)
                self.set(lh.DONE)
                return
            while not self.ctx.cancel.is_set():
                time.sleep(0.02)
            self.set(lh.CANCELLED)
        finally:
            self.close()

    def close(self):
        self.closed = True


def _ctx(max_parallel=4, ram_per=700, tmp=None):
    tmp = tmp or _tmp()
    cfg = {**lh.HUB_DEFAULTS, "max_parallel": max_parallel, "ram_per_session_mb": ram_per}
    return lh.Context(cfg=cfg, bind="127.0.0.1", networks=lh.parse_cidrs("127.0.0.0/8"), vnc_password="Secr3tPw",
                      passwd_file=tmp / "pw", novnc_dir="/x", ports=lh.PortPool(6001, max_parallel + 8, lambda p: True),
                      displays=lh.DisplayPool(), log_dir=tmp / "logs", options=lh.Options(), cancel=threading.Event(),
                      accounts={}, guests={}, guest_cfg={}, login_cfg={})


def _hub(ctx, pending, behaviours=None, free_ram=lambda: 10**6, status_path=None, queue_source=None):
    FakeSession.instances = []
    behaviours = behaviours or {}
    return lh.Hub(ctx, pending, queue_source=queue_source, free_ram=free_ram, status_path=status_path,
                  poll_seconds=0.02,
                  session_factory=lambda c, n, p: FakeSession(c, n, p, behaviours.get(n, "block")))


def test_hub_assigns_ports_in_order_and_respects_ram_limit():
    ctx = _ctx(max_parallel=4)
    hub = _hub(ctx, ["account_11", "account_12", "account_13", "account_14"], free_ram=lambda: 1500)  # 1500//700 = 2
    th = threading.Thread(target=hub.run, daemon=True)
    th.start()
    try:
        assert _wait(lambda: len(FakeSession.instances) == 2)
        time.sleep(0.3)
        assert [(s.account, s.port) for s in FakeSession.instances] == [("account_11", 6001), ("account_12", 6002)]
        assert list(hub.pending) == ["account_13", "account_14"]      # третья не стартует — нет RAM
    finally:
        ctx.cancel.set()
        th.join(timeout=10)
    assert not th.is_alive()


def test_hub_four_parallel_sessions_on_6001_6004():
    ctx = _ctx(max_parallel=4)
    hub = _hub(ctx, ["account_11", "account_12", "account_13", "account_14"])
    th = threading.Thread(target=hub.run, daemon=True)
    th.start()
    try:
        assert _wait(lambda: len(FakeSession.instances) == 4)
        assert sorted(s.port for s in FakeSession.instances) == [6001, 6002, 6003, 6004]
        assert all(s.status == lh.WAITING_LOGIN for s in FakeSession.instances) or _wait(
            lambda: all(s.status == lh.WAITING_LOGIN for s in FakeSession.instances))
    finally:
        ctx.cancel.set()
        th.join(timeout=10)


def test_hub_frees_port_on_success_and_picks_next_from_queue():
    ctx = _ctx(max_parallel=2)
    hub = _hub(ctx, ["account_11", "account_12", "account_13"], behaviours={"account_11": "done"})
    th = threading.Thread(target=hub.run, daemon=True)
    th.start()
    try:
        assert _wait(lambda: len(FakeSession.instances) == 3)
        by = {s.account: s for s in FakeSession.instances}
        assert by["account_11"].status == lh.DONE and by["account_11"].port == 6001
        assert by["account_13"].port == 6001                      # порт освободился и достался следующему
        assert by["account_12"].status == lh.WAITING_LOGIN        # остальные продолжают работать
        assert by["account_12"].port == 6002 and not by["account_12"].closed
    finally:
        ctx.cancel.set()
        th.join(timeout=10)


def test_hub_cancel_closes_all_sessions_and_releases_ports():
    ctx = _ctx(max_parallel=3)
    hub = _hub(ctx, ["account_11", "account_12", "account_13"])
    result = {}
    th = threading.Thread(target=lambda: result.setdefault("rc", hub.run()), daemon=True)
    th.start()
    assert _wait(lambda: len(FakeSession.instances) == 3)
    ctx.cancel.set()                                              # то же, что делает Ctrl+C
    th.join(timeout=10)
    assert not th.is_alive() and result["rc"] == 130
    assert all(s.closed and s.status == lh.CANCELLED for s in FakeSession.instances)
    assert ctx.ports.snapshot() == {} and not hub.running


def test_hub_shutdown_force_closes_hung_session():
    class Hung(FakeSession):
        def run(self):                      # игнорирует отмену, пока его не закроют снаружи
            while not self.closed:
                time.sleep(0.02)

    ctx = _ctx(max_parallel=1)
    hub = lh.Hub(ctx, ["account_11"], poll_seconds=0.02, free_ram=lambda: 10**6,
                 session_factory=lambda c, n, p: Hung(c, n, p))
    FakeSession.instances = []
    th = threading.Thread(target=hub.run, daemon=True)
    th.start()
    assert _wait(lambda: hub.running)
    hub.shutdown(join_timeout=0.3)                                # браузер завис -> принудительное close()
    th.join(timeout=10)
    assert FakeSession.instances[0].closed and ctx.ports.snapshot() == {}


def test_hub_exits_when_ram_is_not_enough_for_one_session():
    ctx = _ctx()
    hub = _hub(ctx, ["account_11"], free_ram=lambda: 100)
    t0 = time.monotonic()
    assert hub.run() == 1 and time.monotonic() - t0 < 5
    assert FakeSession.instances == []


def test_hub_queue_mode_pulls_next_accounts():
    ctx = _ctx(max_parallel=1)
    waiting = ["account_12", "account_13"]
    hub = _hub(ctx, ["account_11"], behaviours={n: "done" for n in ("account_11", "account_12", "account_13")},
               queue_source=lambda busy: [n for n in waiting if n not in busy])
    assert hub.run() == 0
    assert [s.account for s in FakeSession.instances] == ["account_11", "account_12", "account_13"]
    assert all(s.port == 6001 for s in FakeSession.instances)      # один и тот же порт по очереди


def test_hub_status_file_has_ports_and_no_vnc_password():
    tmp = _tmp()
    ctx = _ctx(max_parallel=2, tmp=tmp)
    path = tmp / "status" / "status.json"
    hub = _hub(ctx, ["account_11", "account_12"], status_path=path)
    th = threading.Thread(target=hub.run, daemon=True)
    th.start()
    try:
        assert _wait(lambda: path.exists() and len(json.loads(path.read_text())["sessions"]) == 2)
        raw = path.read_text()
        data = json.loads(raw)
        assert {(s["account"], s["port"]) for s in data["sessions"]} == {("account_11", 6001), ("account_12", 6002)}
        assert "vnc.html?autoconnect=1&resize=scale" in data["sessions"][0]["link"]
        assert ctx.vnc_password not in raw
    finally:
        ctx.cancel.set()
        th.join(timeout=10)


def test_light_snapshot_uses_cookies_only_and_keeps_origins():
    import manual_login as ml

    class Ctx:
        def cookies(self):
            return [{"name": "reddit_session", "value": "x"}]

        def storage_state(self):
            raise AssertionError("storage_state открывает временные страницы — в цикле ожидания не звать")
    first = ml.light_snapshot(Ctx())
    assert first == {"cookies": [{"name": "reddit_session", "value": "x"}], "origins": []}
    prev = {"cookies": [], "origins": [{"origin": "https://www.reddit.com", "localStorage": []}]}
    again = ml.light_snapshot(Ctx(), prev)
    assert again["origins"] == prev["origins"] and again["cookies"][0]["name"] == "reddit_session"
    assert prev["cookies"] == []          # исходный снимок не мутируется
