import json
import os
import stat
import sys
import time
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


class _DumpPage:
    url = "https://www.reddit.com/register/"
    frames = []
    main_frame = None

    def __init__(self, html):
        self.html = html

    def evaluate(self, js):
        return self.html

    def screenshot(self, path):
        open(path, "wb").write(b"png")


def test_autodump_only_on_change_and_capped(tmp_path, monkeypatch):
    import registration_hook as rh
    monkeypatch.setattr(rh, "dump_dir", lambda account: tmp_path / account)
    monkeypatch.setattr(rh, "MAILBOX_ENABLED", False)
    logs = []
    r = rh.HookRunner(rh.RegCtx("a1", "u", "p", "https://x", "http://p", "chrome", log=logs.append))
    page = _DumpPage("<form></form>")
    r.page_ready(page)
    r.tick(page)
    r.tick(page)
    assert len(list((tmp_path / "a1").glob("*.html"))) == 1
    page.html = "<form><input name=email></form>"
    r.tick(page)
    assert len(list((tmp_path / "a1").glob("*.png"))) == 2
    assert "<input name=email>" in sorted((tmp_path / "a1").glob("*.html"))[-1].read_text()
    r._dumps = rh.MAX_DUMPS
    page.html = "<x/>"
    r.tick(page)
    assert len(list((tmp_path / "a1").glob("*.html"))) == 2


class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self

    def _el(self):
        return self.page.els.get(self.sel)

    def count(self):
        return 1 if self._el() is not None else 0

    def is_visible(self):
        return self._el() is not None

    def is_enabled(self):
        return self._el().get("enabled", True)

    def input_value(self):
        return self._el().get("v", "")

    def fill(self, x):
        self._el()["v"] = x

    def click(self, **kw):
        self._el()["clicks"] = self._el().get("clicks", 0) + 1


class _FPage:
    def __init__(self, els):
        self.els = els

    def locator(self, sel):
        return _Loc(self, sel)


def _ctx(tmp_path, monkeypatch, **state):
    import registration_hook as rh
    monkeypatch.setattr(ac, "CREDS_DIR", tmp_path)
    ctx = rh.RegCtx("a1", "Quiet_Owl1", "Pw-123", "https://x", "http://p", "chrome", log=lambda m: None)
    ctx.state.update(state)
    return rh, ctx


def test_step_email_fills_and_clicks_once(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@mailgenerator.net")
    page = _FPage({rh.EMAIL_INPUT: {"v": ""}, rh.EMAIL_SUBMIT: {}})
    assert rh.signup_step(page, ctx) == "email"
    assert page.els[rh.EMAIL_INPUT]["v"] == "x@mailgenerator.net" and page.els[rh.EMAIL_SUBMIT]["clicks"] == 1
    rh.signup_step(page, ctx)
    assert page.els[rh.EMAIL_SUBMIT]["clicks"] == 1          # повтор не раньше CLICK_RETRY_S
    page.els[rh.EMAIL_INPUT]["v"] = "mine@x.com"
    rh.signup_step(page, ctx)
    assert page.els[rh.EMAIL_INPUT]["v"] == "mine@x.com"      # чужое значение не перетираем


def test_step_email_waits_without_mailbox_and_disabled_button(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    page = _FPage({rh.EMAIL_INPUT: {"v": ""}, rh.EMAIL_SUBMIT: {}})
    assert rh.signup_step(page, ctx) == "email_wait" and page.els[rh.EMAIL_INPUT]["v"] == ""
    ctx.state["email"] = "x@y.z"
    page.els[rh.EMAIL_SUBMIT]["enabled"] = False
    rh.signup_step(page, ctx)
    assert "clicks" not in page.els[rh.EMAIL_SUBMIT]


def test_step_code_is_manual(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    logs = []
    ctx.log = logs.append
    page = _FPage({rh.CODE_INPUT: {"v": ""}, rh.EMAIL_INPUT: {"v": ""}})
    assert rh.signup_step(page, ctx) == "code" and rh.signup_step(page, ctx) == "code"
    assert page.els[rh.CODE_INPUT]["v"] == "" and len(logs) == 1


def test_step_username_password(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    page = _FPage({rh.USERNAME_INPUT: {"v": ""}, rh.PASSWORD_INPUT: {"v": ""}, rh.USERNAME_SUBMIT: {}})
    assert rh.signup_step(page, ctx) == "username_password"
    assert page.els[rh.USERNAME_INPUT]["v"] == "Quiet_Owl1" and page.els[rh.PASSWORD_INPUT]["v"] == "Pw-123"
    assert page.els[rh.USERNAME_SUBMIT]["clicks"] == 1


def test_step_age_saves_birthdate_and_confirms(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    els = {rh.AGE_INPUTS[k]: {"v": ""} for k in ("day", "month", "year")}
    els[rh.AGE_SUBMIT] = {}
    page = _FPage(els)
    assert rh.signup_step(page, ctx) == "age"
    bd = ctx.state["birthdate"]
    assert 1 <= bd["day"] <= 28 and 1 <= bd["month"] <= 12
    assert els[rh.AGE_INPUTS["year"]]["v"] == str(bd["year"]) and els[rh.AGE_SUBMIT]["clicks"] == 1
    page2 = _FPage({rh.AGE_CONFIRM: {}})
    assert rh.signup_step(page2, ctx) == "age_confirm" and page2.els[rh.AGE_CONFIRM]["clicks"] == 1
    assert rh.signup_step(_FPage({}), ctx) is None


def test_birthdate_is_adult():
    import registration_hook as rh
    for _ in range(50):
        bd = rh.make_birthdate()
        assert time.gmtime().tm_year - bd["year"] >= 22 and bd["day"] <= 28


class _MPage:
    def __init__(self, email, loginwrap):
        self.email, self.loginwrap = email, loginwrap
        self.url = "https://mailgenerator.net"
        self.frames = []

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        return "<x/>"

    def screenshot(self, path):
        pass

    def locator(self, sel):
        outer = self

        class L:
            first = property(lambda s: s)

            def click(s, **k):
                if sel != "#cookieRejectBtn":
                    raise AssertionError(sel)

            def text_content(s, **k):
                return {"#activeEmail": outer.email, "#loginWrap": outer.loginwrap}[sel]
        return L()


def _mailbox_page(mp):
    class Ctxp:
        def new_page(self):
            return mp

    class Page:
        context = Ctxp()
    return Page()


def test_get_mailbox_reads_address_and_password(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(rh, "dump_dir", lambda account: tmp_path / "d" / account)
    mp = _MPage(" abc123@mailgenerator.net ", "Login Email: x Password: s3cret!Z more")
    assert rh.get_mailbox(_mailbox_page(mp), ctx) == "abc123@mailgenerator.net"
    saved = json.loads(ac.creds_path("a1", tmp_path).read_text())
    assert saved["email"] == "abc123@mailgenerator.net" and saved["mailbox_password"] == "s3cret!Z"


def test_get_mailbox_failure_returns_none(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(rh, "dump_dir", lambda account: tmp_path / "d" / account)
    logs = []
    ctx.log = logs.append
    assert rh.get_mailbox(_mailbox_page(_MPage("", "")), ctx) is None
    assert any("не прочитана" in m for m in logs)


def test_new_page_failure_propagates_to_runner(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)

    class Ctxp:
        def new_page(self):
            raise RuntimeError("no browser")

    class Page:
        context = Ctxp()
    try:
        rh.get_mailbox(Page(), ctx)
        raised = False
    except RuntimeError:
        raised = True
    assert raised  # исключение поймает HookRunner._call
