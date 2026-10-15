"""Диагностика харвестера гостевых cookies: классификация ошибок навигации,
предпроверка порта (браузер не запускается, если порт мёртв), разбор ответа
403 и проба из настоящего браузера. Playwright/curl_cffi подменяются
заглушками, браузеры не запускаются."""
import importlib
import socket
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


def test_classify_nav_error_real_messages():
    cases = {
        "net::ERR_PROXY_CONNECTION_FAILED at https://www.reddit.com/": "proxy_down",
        "NS_ERROR_PROXY_CONNECTION_REFUSED": "proxy_down",
        "Could not connect to the server.": "proxy_down",              # webkit
        "net::ERR_TUNNEL_CONNECTION_FAILED": "node",
        "net::ERR_CONNECTION_RESET": "node",
        "net::ERR_EMPTY_RESPONSE": "node",
        "NS_ERROR_NET_RESET": "node",
        "net::ERR_TIMED_OUT": "node",
        "Page.goto: Target page, context or browser has been closed": "browser",
        "Page crashed": "browser",
        "что-то совсем другое": "other",
        "": "other",
    }
    for msg, kind in cases.items():
        assert rgc.classify_nav_error(msg) == kind, (msg, rgc.classify_nav_error(msg), kind)
    assert set(rgc.NAV_HINTS) >= {"browser", "proxy_down", "node", "other"}


def test_check_proxy_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert rgc.check_proxy_port("127.0.0.1", port) is None
    finally:
        srv.close()
    err = rgc.check_proxy_port("127.0.0.1", port, timeout=1)      # теперь закрыт
    assert isinstance(err, str) and err


def test_dead_port_skips_browser_launch():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    class ExplodingPW:
        """Любое обращение к движку = браузер запускается, чего быть не должно."""
        def __getattr__(self, name):
            raise AssertionError(f"браузер запущен ({name}), хотя порт мёртв")

    cfg = {"guest_accounts": {"account_30": {"engine": "chromium"}}}
    account = {"name": "account_30", "cookie_file": "cookies/account_30.json", "proxy_port": dead_port,
               "impersonate": "chrome131", "user_agent": "UA"}
    orig = rgc.PROXY_HOST
    rgc.PROXY_HOST = "127.0.0.1"
    try:
        res = rgc.process_guest_account(ExplodingPW(), account, cfg, dry_run=True)
    finally:
        rgc.PROXY_HOST = orig
    assert res.ok is False and "не принимает соединения" in res.detail
    assert str(dead_port) in res.detail and "generate_mihomo_config" in res.detail


class FakeResp:
    def __init__(self, status, headers, text):
        self.status_code, self.headers, self.text = status, headers, text


def test_failure_verdict_distinguishes_causes():
    assert "отпечатк" in rgc.failure_verdict(200)           # браузеру можно, curl нельзя
    for st in (401, 403, 429):
        assert "заблокирована" in rgc.failure_verdict(st)   # закрыто и браузеру
    assert "неоднозначна" in rgc.failure_verdict(None)
    assert "неоднозначна" in rgc.failure_verdict("ERR")


def test_describe_http_failure_has_status_headers_bodies_and_verdict():
    resp = FakeResp(403, {"Server": "snooserv", "CF-Ray": "abc", "Content-Type": "text/html", "X-Other": "no"},
                    "<html>\n  whoa   there, pardner!  </html>" + "x" * 500)
    text = rgc.describe_http_failure(resp, 200, "{'kind': 'Listing'}")
    assert "curl_cffi: 403" in text and "server=snooserv" in text and "cf-ray=abc" in text
    assert "x-other" not in text                             # лишние заголовки не выводим
    assert "whoa there, pardner!" in text                    # пробелы/переводы строк схлопнуты
    assert len(text) < 900                                   # тело обрезано, лог не раздувается
    assert "из браузера: 200" in text and "ВЫВОД" in text and "отпечатк" in text


def test_describe_http_failure_survives_broken_response():
    class Broken(FakeResp):
        @property
        def text(self):
            raise RuntimeError("decode")

        @text.setter
        def text(self, v):
            pass

    out = rgc.describe_http_failure(Broken(403, {}, ""), None, "")
    assert "<нечитаемо>" in out and "curl_cffi: 403" in out


def test_browser_fetch_probe():
    class Page:
        def __init__(self, result=None, exc=None):
            self.result, self.exc, self.args = result, exc, None

        def evaluate(self, js, arg):
            self.args = (js, arg)
            if self.exc:
                raise self.exc
            return self.result

    p = Page([403, "  {\n 'message': 'Forbidden' } "])
    status, body = rgc.browser_fetch_probe(p, "https://www.reddit.com/r/AskReddit/comments.json")
    assert status == 403 and body == "{ 'message': 'Forbidden' }"
    assert "fetch(" in p.args[0] and p.args[1].endswith("comments.json")
    status, body = rgc.browser_fetch_probe(Page(exc=RuntimeError("page closed")), "u")
    assert status is None and "проба из браузера не удалась" in body
