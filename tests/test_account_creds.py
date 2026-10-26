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

    def press(self, key):
        self._el().setdefault("pressed", []).append(key)


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


class _Mail:
    """Вкладка почты: текст списка, текст письма, счётчик перезагрузок."""
    def __init__(self, lst="Your inbox is waiting", body=""):
        self.lst, self.body, self.reloads, self.opened = lst, body, 0, 0

    def reload(self, **k):
        self.reloads += 1

    def locator(self, sel):
        outer = self

        class L:
            first = property(lambda s: s)

            def inner_text(s, **k):
                return outer.lst if sel == "#messageList" else outer.body

            def get_by_text(s, rx):
                return s

            def click(s, **k):
                if sel == "#messageList":
                    outer.opened += 1
        return L()


def test_step_code_fills_presses_enter_and_prints_link(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    logs = []
    ctx.log = logs.append
    mail = _Mail()
    ctx.state["mail_page"] = mail
    ctx.link = "http://h:1/vnc.html"
    front = []
    page = _FPage({rh.CODE_INPUT: {"v": ""}, rh.EMAIL_INPUT: {"v": ""}})
    page.bring_to_front = lambda: front.append(1)
    assert rh.signup_step(page, ctx) == "code"            # письма ещё нет
    assert not front and not logs
    mail.lst = "Reddit  482913 is your Reddit verification code"
    assert rh.signup_step(page, ctx) == "code" and rh.signup_step(page, ctx) == "code"
    assert sum("482913" in m and "регистрация продолжается" in m and "http://h:1/vnc" in m for m in logs) == 1 and front == [1]
    assert page.els[rh.CODE_INPUT]["v"] == "482913" and page.els[rh.CODE_INPUT]["clicks"] == 1
    assert page.els[rh.CODE_INPUT]["pressed"] == ["Enter"]
    mail.lst = "Reddit  111222 is your Reddit verification code"      # повторная отправка: новый код вместо старого
    rh.signup_step(page, ctx)
    assert any("111222" in m for m in logs) and page.els[rh.CODE_INPUT]["v"] == "111222"
    assert page.els[rh.CODE_INPUT]["pressed"] == ["Enter", "Enter"]


def test_read_mail_code_opens_message_when_list_has_no_code(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    ctx.state["mail_page"] = _Mail(lst="Reddit  Verify your email", body="Your code: 654321. Order 1234567")
    assert rh.read_mail_code(ctx) == "654321" and ctx.state["mail_page"].opened == 1
    assert rh.read_mail_code(rh.RegCtx("a", "u", "p", "x", "y", "c")) is None


def test_read_mail_code_reloads_tab_periodically(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    mail = ctx.state["mail_page"] = _Mail()
    t = [1000.0]
    monkeypatch.setattr(rh, "_now", lambda: t[0])
    assert rh.read_mail_code(ctx) is None and mail.reloads == 0
    t[0] += rh.MAIL_RELOAD_S + 1
    rh.read_mail_code(ctx)
    assert mail.reloads == 1


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
    assert ctx.state["mail_page"] is mp
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


class _EPage(_FPage):
    """Страница шага почты; get_by_text(...) видит ошибку, если в els есть ключ "text:error"."""
    def get_by_text(self, rx):
        return _Loc(self, "text:error")

    def get_by_role(self, role, name=None):
        for key, text in (("role:cookies", "Optionale Cookies ablehnen"), ("role:login", "Log In"),
                          ("role:signup", "Sign up")):
            if name is not None and name.search(text):
                return _Loc(self, key)
        return _Loc(self, "role:none")


def test_email_send_error_retries_three_times_then_requests_new_identity(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    logs = []
    ctx.log = logs.append
    t = [100.0]
    monkeypatch.setattr(rh, "_now", lambda: t[0])
    page = _EPage({rh.EMAIL_INPUT: {"v": ""}, rh.EMAIL_SUBMIT: {}})
    assert rh.signup_step(page, ctx) == "email" and page.els[rh.EMAIL_SUBMIT]["clicks"] == 1

    page.els[rh.EMAIL_ERROR_BANNER] = {}                    # Reddit ответил ошибкой отправки кода
    t[0] += 3
    rh.signup_step(page, ctx)
    assert page.els[rh.EMAIL_SUBMIT]["clicks"] == 1         # рано: ответ на ЭТУ попытку ещё не пришёл
    for expected in (2, 3):
        t[0] += rh.ERROR_WAIT_S
        assert rh.signup_step(page, ctx) == "email"
        assert page.els[rh.EMAIL_SUBMIT]["clicks"] == expected
        assert not ctx.state.get("replace_identity")
    t[0] += rh.ERROR_WAIT_S                                  # третья попытка тоже с ошибкой
    assert rh.signup_step(page, ctx) == "email_failed"
    assert ctx.state["replace_identity"] and page.els[rh.EMAIL_SUBMIT]["clicks"] == 3
    t[0] += 100
    assert rh.signup_step(page, ctx) == "email_failed" and page.els[rh.EMAIL_SUBMIT]["clicks"] == 3
    assert sum("пробую ещё раз" in m for m in logs) == 2 and any("меняю ноду и профиль" in m for m in logs)


def test_email_error_found_by_text_and_no_error_means_no_retry(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    t = [100.0]
    monkeypatch.setattr(rh, "_now", lambda: t[0])
    page = _EPage({rh.EMAIL_INPUT: {"v": ""}, rh.EMAIL_SUBMIT: {}})
    rh.signup_step(page, ctx)
    t[0] += rh.ERROR_WAIT_S + 1                              # ошибки нет — повтор только по таймеру CLICK_RETRY_S
    rh.signup_step(page, ctx)
    assert page.els[rh.EMAIL_SUBMIT]["clicks"] == 1 and "email_fails" not in ctx.state
    page.els["text:error"] = {}                              # ошибка без баннера: нашлась по тексту
    rh.signup_step(page, ctx)
    assert ctx.state["email_fails"] == 1 and page.els[rh.EMAIL_SUBMIT]["clicks"] == 2


def test_send_error_regex_matches_german_message():
    import registration_hook as rh
    assert rh.SEND_ERROR_RE.search("Beim Senden des Verifizierungscodes ist etwas schiefgelaufen.")
    assert not rh.SEND_ERROR_RE.search("Gib deine E-Mail-Adresse ein")


def test_email_step_stuck_without_recognized_error_still_counts_and_swaps(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    t = [100.0]
    monkeypatch.setattr(rh, "_now", lambda: t[0])
    page = _EPage({rh.EMAIL_INPUT: {"v": ""}, rh.EMAIL_SUBMIT: {}})       # ни баннера, ни текста ошибки
    rh.signup_step(page, ctx)
    for _ in range(3):
        t[0] += rh.STUCK_S
        rh.signup_step(page, ctx)
    assert ctx.state["replace_identity"] and page.els[rh.EMAIL_SUBMIT]["clicks"] == 3
    clicks = page.els[rh.EMAIL_SUBMIT]["clicks"]
    t[0] += 1000
    rh.signup_step(page, ctx)
    assert page.els[rh.EMAIL_SUBMIT]["clicks"] == clicks                   # больше не жмём


def test_cookie_banner_is_rejected_once_in_a_while(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    t = [100.0]
    monkeypatch.setattr(rh, "_now", lambda: t[0])
    page = _EPage({"role:cookies": {}})
    assert rh.dismiss_cookies(page, ctx) and page.els["role:cookies"]["clicks"] == 1
    assert not rh.dismiss_cookies(page, ctx)                               # пауза 10 с
    t[0] += 11
    assert rh.dismiss_cookies(page, ctx) and page.els["role:cookies"]["clicks"] == 2
    assert not rh.dismiss_cookies(_EPage({}), ctx)
    assert rh.COOKIE_BTN_RE.search("Optionale Cookies ablehnen")


def test_open_signup_from_home_login_then_signup_then_form(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch, email="x@y.z")
    t = [100.0]
    monkeypatch.setattr(rh, "_now", lambda: t[0])
    page = _EPage({"role:login": {}})                         # главная: синяя кнопка Log In
    assert rh.signup_step(page, ctx) == "open_login" and page.els["role:login"]["clicks"] == 1
    assert not ctx.state.get("form_seen")
    page.els["role:signup"] = {}                              # открылось окно входа со ссылкой Sign up
    assert rh.signup_step(page, ctx) is None                  # пауза 5 с между попытками
    t[0] += 6
    assert rh.signup_step(page, ctx) == "open_signup" and page.els["role:signup"]["clicks"] == 1
    page.els[rh.EMAIL_INPUT] = {"v": ""}
    page.els[rh.EMAIL_SUBMIT] = {}
    t[0] += 6
    assert rh.signup_step(page, ctx) == "email" and ctx.state["form_seen"]
    t[0] += 60
    page.els.pop(rh.EMAIL_INPUT)                              # форма видна не всегда: Log In больше не жмём
    rh.signup_step(page, ctx)
    assert page.els["role:login"]["clicks"] == 1


def test_open_signup_without_login_button_goes_straight_to_signup(tmp_path, monkeypatch):
    rh, ctx = _ctx(tmp_path, monkeypatch)
    page = _EPage({"role:signup": {}})
    assert rh.signup_step(page, ctx) == "open_signup" and page.els["role:signup"]["clicks"] == 1
    assert rh.LOGIN_BTN_RE.search("Log In") and not rh.LOGIN_BTN_RE.search("Log in with Google")
    assert rh.SIGNUP_BTN_RE.search("Sign Up") and not rh.SIGNUP_BTN_RE.search("Sign up with Apple")
