"""scripts/fetch_free_proxies.py: разбор списка ProxyScrape, проверка доступности, запись и подключение к пулу."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import fetch_free_proxies as ff  # noqa: E402
import generate_mihomo_config as gmc  # noqa: E402
import rotation as rot  # noqa: E402

SAMPLE = """http://1.1.1.1:80
https://2.2.2.2:443
socks4://3.3.3.3:1080
socks5://4.4.4.4:1081
http://1.1.1.1:80
ftp://5.5.5.5:21
6.6.6.6:8080
garbage line
http://7.7.7.7:99999
"""


def test_parse_list_keeps_http_https_socks5_and_counts_the_rest():
    uris, st = ff.parse_list(SAMPLE)
    assert uris == ["http://1.1.1.1:80", "http://2.2.2.2:443", "socks5://4.4.4.4:1081", "http://6.6.6.6:8080"]
    assert st == {"lines": 9, "kept": 4, "socks4": 1, "other": 1, "junk": 2, "dups": 1}


def test_check_proxy_stages_and_lenient():
    calls = []

    def mk(neutral, reddit):
        def get(url, uri, timeout):
            calls.append(url)
            r = neutral if url == ff.NEUTRAL_URL else reddit
            if isinstance(r, Exception):
                raise r
            return r
        return get
    ok = ff.check_proxy("http://a:1", get=mk(200, 200))
    assert ok["ok"] and ok["stage"] == "ok" and ok["ms"] is not None
    dead = ff.check_proxy("http://a:1", get=mk(OSError("x"), 200))
    assert not dead["ok"] and dead["stage"] == "neutral"
    blocked = ff.check_proxy("http://a:1", get=mk(200, 403))
    assert not blocked["ok"] and blocked["stage"] == "reddit" and blocked["status"] == 403
    assert ff.check_proxy("http://a:1", lenient=True, get=mk(200, 403))["ok"]
    assert not ff.check_proxy("http://a:1", lenient=True, get=mk(200, 502))["ok"]
    assert not ff.check_proxy("http://a:1", get=mk(407, 200))["ok"]


def test_socks5_goes_through_socks5h():
    assert ff._proxies("socks5://1.2.3.4:1") == {"http": "socks5h://1.2.3.4:1", "https": "socks5h://1.2.3.4:1"}
    assert ff._proxies("http://1.2.3.4:1") == {"http": "http://1.2.3.4:1", "https": "http://1.2.3.4:1"}


def test_main_writes_free_file_that_joins_the_pool(monkeypatch=None, capsys=None):
    d = Path(tempfile.mkdtemp())
    src = d / "list.txt"
    src.write_text(SAMPLE, encoding="utf-8")
    (d / "all_nodes.txt").write_text("vless://00000000-0000-4000-8000-000000000001@v.example.net:443?security=tls#us1\n",
                                     encoding="utf-8")
    fast = {"http://1.1.1.1:80", "socks5://4.4.4.4:1081"}
    orig = ff.check_all
    ff.check_all = lambda uris, workers, timeout, lenient, progress=None, get=None: [
        {"uri": u, "ok": u in fast, "ms": 100 if u.startswith("http://1.") else 50, "stage": "ok" if u in fast else "reddit",
         "status": 200} for u in uris]
    argv = sys.argv
    try:
        sys.argv = ["x", "--file", str(src), "--nodes-file", str(d / "all_nodes.txt")]
        assert ff.main() == 0 and not (d / "all_proxys.free.txt").exists()       # без --apply ничего не пишется
        sys.argv += ["--apply"]
        assert ff.main() == 0
    finally:
        ff.check_all, sys.argv = orig, argv
    text = (d / "all_proxys.free.txt").read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    assert lines == ["socks5://4.4.4.4:1081#free-4.4.4.4-1081", "http://1.1.1.1:80#free-1.1.1.1-80"]   # быстрые первыми
    pool = rot.load_pools(d / "all_nodes.txt")
    assert [gmc.link_kind(n.uri) for n in pool] == ["vless", "proxy", "proxy"]
    assert [n.name for n in pool][1:] == ["free-4.4.4.4-1081", "free-1.1.1.1-80"]
    assert gmc.parse_proxy(pool[1].uri)["type"] == "socks5" and gmc.parse_proxy(pool[2].uri)["type"] == "http"
    assert rot.pools_exist(d / "all_nodes.txt")
    assert rot.pools_exist(Path(tempfile.mkdtemp()) / "none.txt", "none2.txt") is False
    only_free = Path(tempfile.mkdtemp())
    (only_free / "all_proxys.free.txt").write_text(text, encoding="utf-8")
    assert rot.pools_exist(only_free / "all_nodes.txt")


def test_empty_result_does_not_overwrite_existing_file():
    d = Path(tempfile.mkdtemp())
    (d / "all_nodes.txt").write_text("", encoding="utf-8")
    (d / "all_proxys.free.txt").write_text("http://9.9.9.9:1#free-9.9.9.9-1\n", encoding="utf-8")
    src = d / "l.txt"
    src.write_text("http://1.1.1.1:80\n", encoding="utf-8")
    orig = ff.check_all
    ff.check_all = lambda uris, *a, **k: [{"uri": u, "ok": False, "ms": None, "stage": "neutral", "status": None} for u in uris]
    argv = sys.argv
    try:
        sys.argv = ["x", "--file", str(src), "--nodes-file", str(d / "all_nodes.txt"), "--apply"]
        assert ff.main() == 1
    finally:
        ff.check_all, sys.argv = orig, argv
    assert (d / "all_proxys.free.txt").read_text(encoding="utf-8").startswith("http://9.9.9.9")
