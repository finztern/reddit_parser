"""Тесты параллельного режима scripts/refresh_guest_cookies.py.

Playwright и curl_cffi подменяются заглушками (если их нет в окружении),
браузеры не запускаются: проверяем раскладку слотов по воркерам, выбор
числа воркеров по RAM/CPU, защиту по памяти и сборку команд подпроцессов.
"""
import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _stub_if_missing(name, **attrs):
    try:
        importlib.import_module(name)
        return
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


def _load():
    _stub_if_missing("playwright")
    _stub_if_missing("playwright.sync_api", TimeoutError=TimeoutError, sync_playwright=lambda: None)
    _stub_if_missing("curl_cffi")
    _stub_if_missing("curl_cffi.requests", Session=object, get=lambda *a, **k: None)
    if "curl_cffi" in sys.modules and not hasattr(sys.modules["curl_cffi"], "requests"):
        sys.modules["curl_cffi"].requests = sys.modules["curl_cffi.requests"]
    return importlib.import_module("refresh_guest_cookies")


rgc = _load()

CFG = {
    "guest_accounts": {},
    "parallel_browsers": 6,
    "browser_memory_mb": 600,
    "memory_reserve_mb": 1000,
}


def _accounts(spec):
    """spec: {'account_26': 'chrome131', ...} -> accounts + guest_accounts в cfg."""
    cfg = {**CFG, "guest_accounts": {}}
    accs = []
    for name, imp in spec.items():
        accs.append({"name": name, "impersonate": imp})
        cfg["guest_accounts"][name] = {}
    return accs, cfg


# ---------- число воркеров ----------

def test_workers_limited_by_memory():
    # 5000 МБ свободно, резерв 1000, 600 на браузер -> (5000-1000)//600 = 6
    assert rgc.pick_worker_count(None, 55, CFG, 5000, 16) == 6
    # 3000 МБ -> (3000-1000)//600 = 3
    assert rgc.pick_worker_count(None, 55, CFG, 3000, 16) == 3
    # почти нет памяти -> всё равно хотя бы один воркер
    assert rgc.pick_worker_count(None, 55, CFG, 900, 16) == 1


def test_workers_limited_by_cpu_cap_and_slots():
    assert rgc.pick_worker_count(None, 55, CFG, 100000, 2) == 2       # 2 CPU
    assert rgc.pick_worker_count(None, 55, CFG, 100000, 32) == 6      # потолок parallel_browsers
    assert rgc.pick_worker_count(None, 3, CFG, 100000, 32) == 3       # не больше слотов


def test_workers_explicit_and_unknown_memory():
    assert rgc.pick_worker_count(1, 55, CFG, 100000, 32) == 1         # --workers 1
    assert rgc.pick_worker_count(10, 55, CFG, 500, 1) == 10           # явное число не режем по RAM/CPU
    assert rgc.pick_worker_count(10, 4, CFG, 500, 1) == 4             # но и не больше слотов
    assert rgc.pick_worker_count(None, 55, CFG, None, 8) == 6         # RAM неизвестна -> потолок


# ---------- раскладка по воркерам ----------

def test_split_covers_every_slot_exactly_once():
    spec = {f"account_{n}": ("firefox144" if n % 4 == 0 else "safari260" if n % 3 == 0 else "chrome131")
            for n in range(11, 56)}
    accs, cfg = _accounts(spec)
    shards = rgc.split_shards(accs, cfg, 6)
    names = [a["name"] for sh in shards for a in sh]
    assert sorted(names) == sorted(spec)
    assert len(names) == len(set(names))
    assert len(shards) == 6


def test_split_balances_heavy_engines():
    # 6 тяжёлых (firefox/webkit) и 6 лёгких (chromium) на 3 воркера: тяжёлые не должны
    # скопиться в одном воркере
    spec = {}
    for i in range(6):
        spec[f"account_{20 + i}"] = "firefox144" if i % 2 else "safari260"
    for i in range(6):
        spec[f"account_{30 + i}"] = "chrome131"
    accs, cfg = _accounts(spec)
    shards = rgc.split_shards(accs, cfg, 3)
    heavy_per_shard = [sum(1 for a in sh if not a["impersonate"].startswith("chrome")) for sh in shards]
    assert max(heavy_per_shard) - min(heavy_per_shard) <= 1
    for sh in shards:  # внутри воркера — по номеру слота
        nums = [rgc._acc_num(a["name"]) for a in sh]
        assert nums == sorted(nums)


def test_split_never_returns_empty_shards():
    accs, cfg = _accounts({"account_26": "chrome131", "account_27": "chrome131"})
    shards = rgc.split_shards(accs, cfg, 6)
    assert len(shards) == 2 and all(shards)


# ---------- защита по памяти ----------

def test_wait_for_memory_ok_unknown_and_low(monkeypatch=None):
    orig_read, orig_sleep = rgc.read_mem_available_mb, rgc.time.sleep
    try:
        rgc.read_mem_available_mb = lambda: 4000
        assert rgc.wait_for_memory({"min_free_memory_mb": 700, "memory_wait_seconds": 0}, "a") is True
        rgc.read_mem_available_mb = lambda: None                       # не Linux
        assert rgc.wait_for_memory({"min_free_memory_mb": 700, "memory_wait_seconds": 0}, "a") is True
        rgc.read_mem_available_mb = lambda: 100
        assert rgc.wait_for_memory({"min_free_memory_mb": 0, "memory_wait_seconds": 0}, "a") is True  # выключено
        rgc.time.sleep = lambda s: None
        assert rgc.wait_for_memory({"min_free_memory_mb": 700, "memory_wait_seconds": 0}, "a") is False
    finally:
        rgc.read_mem_available_mb, rgc.time.sleep = orig_read, orig_sleep


def test_run_pass_defers_slot_when_no_memory():
    calls = []
    orig_wait, orig_proc = rgc.wait_for_memory, rgc.process_guest_account
    try:
        rgc.wait_for_memory = lambda cfg, name: name != "account_27"
        rgc.process_guest_account = lambda pw, acc, cfg, dry: (
            calls.append(acc["name"]) or rgc.AccountResult(acc["name"], True, "ok"))
        res = rgc._run_pass(None, [{"name": "account_26"}, {"name": "account_27"}],
                            {"stagger_seconds": 0}, False)
    finally:
        rgc.wait_for_memory, rgc.process_guest_account = orig_wait, orig_proc
    assert calls == ["account_26"]                      # браузер для account_27 не запускали
    assert [(r.name, r.ok) for r in res] == [("account_26", True), ("account_27", False)]


# ---------- подпроцессы ----------

def test_run_parallel_builds_one_subprocess_per_shard():
    started = []

    class FakeProc:
        def __init__(self, cmd, env=None):
            started.append((cmd, env))
            self.rc = 0

        def poll(self):
            return self.rc

        def wait(self):
            return self.rc

        def terminate(self):
            pass

    orig = rgc.subprocess.Popen
    rgc.subprocess.Popen = FakeProc
    try:
        shards = [[{"name": "account_26"}, {"name": "account_28"}], [{"name": "account_27"}]]
        cfg = {"headless": False, "worker_start_stagger_seconds": 0}
        rc = rgc.run_parallel(shards, cfg, Path("/x/guest.yaml"), dry_run=True)
    finally:
        rgc.subprocess.Popen = orig
    assert rc == 0 and len(started) == 2
    cmd0, env0 = started[0]
    assert cmd0[2:4] == ["--workers", "1"]               # дети НЕ плодят внуков
    assert "--no-headless" in cmd0 and "--dry-run" in cmd0
    assert cmd0.count("--account") == 2 and "account_26" in cmd0 and "account_28" in cmd0
    assert env0["GUEST_SHARD"] == "1/2" and started[1][1]["GUEST_SHARD"] == "2/2"


def test_run_parallel_propagates_child_failure():
    class FakeProc:
        n = 0

        def __init__(self, cmd, env=None):
            FakeProc.n += 1
            self.rc = 1 if FakeProc.n == 2 else 0

        def poll(self):
            return self.rc

        def wait(self):
            return self.rc

        def terminate(self):
            pass

    orig = rgc.subprocess.Popen
    rgc.subprocess.Popen = FakeProc
    try:
        rc = rgc.run_parallel([[{"name": "a_1"}], [{"name": "a_2"}]],
                              {"headless": True, "worker_start_stagger_seconds": 0}, Path("c.yaml"), False)
    finally:
        rgc.subprocess.Popen = orig
    assert rc == 1
