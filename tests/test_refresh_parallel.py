import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    if "refresh_cookies" in sys.modules:
        return sys.modules["refresh_cookies"]
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import playwright.sync_api  # noqa: F401  (настоящий Playwright есть — заглушки не нужны)
        import refresh_cookies as rc
        return rc
    except ImportError:
        pass
    pw = types.ModuleType("playwright.sync_api")

    class _Cm:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False
    pw.sync_playwright = lambda: _Cm()
    pw.TimeoutError = TimeoutError
    saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.sync_api")}
    sys.modules["playwright"], sys.modules["playwright.sync_api"] = types.ModuleType("playwright"), pw
    try:
        import refresh_cookies as rc
    finally:  # заглушки не должны утечь в другие тесты
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return rc


class _FakePw:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _fake(rc):
    rc.sync_playwright = lambda: _FakePw()   # без запуска настоящего драйвера Playwright
    return rc


def test_parallel_runs_concurrently_keeps_order_and_isolates_errors():
    rc = _fake(_load())
    accounts = [{"name": f"a{i}"} for i in range(6)]
    cur, peak, lock = [0], [0], threading.Lock()

    def process(pw, account, cfg, dry, human):
        with lock:
            cur[0] += 1
            peak[0] = max(peak[0], cur[0])
        time.sleep(0.15)
        with lock:
            cur[0] -= 1
        if account["name"] == "a2":
            raise RuntimeError("boom")
        return rc.AccountResult(account["name"], True, "ok")

    t = time.monotonic()
    res = rc.run_accounts(accounts, {}, False, None, 3, {}, process=process)
    assert time.monotonic() - t < 0.6          # 6 × 0.15 с последовательно = 0.9 с
    assert peak[0] == 3
    assert [r.name for r in res] == [a["name"] for a in accounts]
    assert [r.ok for r in res] == [True, True, False, True, True, True]
    assert "boom" in res[2].detail


def test_sequential_when_one_worker():
    rc = _fake(_load())
    seen = []
    res = rc.run_accounts([{"name": "x"}, {"name": "y"}], {}, False, None, 1, {},
                          process=lambda pw, a, *r: seen.append(a["name"]) or rc.AccountResult(a["name"], True, ""))
    assert seen == ["x", "y"] and len(res) == 2


def test_offsets_delay_start():
    rc = _fake(_load())
    starts = {}
    t0 = time.monotonic()

    def process(pw, a, *r):
        starts[a["name"]] = time.monotonic() - t0
        return rc.AccountResult(a["name"], True, "")
    rc.run_accounts([{"name": "x"}, {"name": "y"}], {}, False, None, 2, {"x": 0.0, "y": 0.3}, process=process)
    assert starts["y"] >= 0.28 and starts["x"] < 0.2


def test_plan_offsets():
    rc = _fake(_load())
    assert rc.plan_offsets(["a", "b", "c"], False, 10) == {"a": 0, "b": 10, "c": 20}
    assert rc.plan_offsets(["a", "b"], False, 0) == {"a": 0.0, "b": 0.0}
    out = rc.plan_offsets(["a", "b", "c"], True, 10)
    assert sorted(out) == ["a", "b", "c"] and min(out.values()) == 0
