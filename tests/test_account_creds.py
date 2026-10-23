import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import account_creds as ac  # noqa: E402


def test_username_and_password_shape():
    for _ in range(200):
        assert ac.USERNAME_RE.fullmatch(ac.make_username())
        pw = ac.make_password()
        assert len(pw) >= 16
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(c in "-_!#%*+=?" for c in pw)
        assert not set(pw) & set("0OIl1")


def test_prepare_persists_reuses_and_regenerates(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "username_available", lambda *a, **k: True)
    d1, reused = ac.prepare("account_1", None, base=tmp_path / "c")
    assert not reused and d1["username_checked"] is True
    p = ac.creds_path("account_1", tmp_path / "c")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    d2, reused = ac.prepare("account_1", None, base=tmp_path / "c")
    assert reused and d2["password"] == d1["password"]
    d3, reused = ac.prepare("account_1", None, new=True, base=tmp_path / "c")
    assert not reused and d3["password"] != d1["password"]


def test_taken_usernames_skipped(tmp_path, monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(ac, "username_available", lambda *a, **k: next(answers))
    d, _ = ac.prepare("account_2", None, base=tmp_path)
    assert d["username_checked"] is True


def test_unknown_availability_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "username_available", lambda *a, **k: None)
    d, _ = ac.prepare("account_3", None, base=tmp_path)
    assert d["username_checked"] is None and d["username"]


def test_availability_parsing(monkeypatch):
    for status, text, want in ((200, "true", True), (200, "false\n", False), (403, "true", None), (200, "x", None)):
        monkeypatch.setattr(ac, "_http_get", lambda *a, _s=status, _t=text: (_s, _t))
        assert ac.username_available("abc", None) is want

    def boom(*a):
        raise OSError
    monkeypatch.setattr(ac, "_http_get", boom)
    assert ac.username_available("abc", None) is None


def test_mark_logged_in(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "username_available", lambda *a, **k: True)
    ac.prepare("account_4", None, base=tmp_path)
    ac.mark_logged_in("account_4", "RealName", base=tmp_path)
    cur = json.loads(ac.creds_path("account_4", tmp_path).read_text())
    assert cur["username"] == "RealName" and cur["status"] == "logged_in"


def test_hook_runner_is_safe_and_disables(monkeypatch):
    import registration_hook as rh
    logs = []
    ctx = rh.RegCtx("a", "u", "p", "https://x", "http://p", "chrome", log=logs.append)
    calls = {"ready": 0, "tick": 0}

    def ready(page, c):
        calls["ready"] += 1
        raise RuntimeError("boom")

    def tick(page, c):
        calls["tick"] += 1
        raise ValueError("bad")
    ready.__name__, tick.__name__ = "on_page_ready", "on_tick"
    monkeypatch.setattr(rh, "on_page_ready", ready)
    monkeypatch.setattr(rh, "on_tick", tick)
    r = rh.HookRunner(ctx)
    r.page_ready(None)
    r.page_ready(None)
    assert calls["ready"] == 1
    for _ in range(40):
        r.tick(None)
    assert calls["tick"] == rh.HookRunner.MAX_TICK_FAILS
    assert sum("on_tick:" in m for m in logs) == 1 and any("отключён" in m for m in logs)


def test_hook_default_is_noop():
    import registration_hook as rh
    ctx = rh.RegCtx("a", "u", "p", "https://x", "http://p", "chrome", log=lambda m: None)
    r = rh.HookRunner(ctx)
    r.page_ready(object())
    r.tick(object())
