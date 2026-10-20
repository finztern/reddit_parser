"""Прокси-only: разбор all_nodes.txt, имена, гео по выходному IP, выбор самых быстрых.
Все адреса и логины — вымышленные (example.net)."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import add_guest_slots as ags  # noqa: E402
import filter_nodes as fn  # noqa: E402
import generate_mihomo_config as gmc  # noqa: E402
import rotation as rot  # noqa: E402


def test_normalize_formats():
    n = gmc.normalize_proxy
    assert n("u:p@h.example.net:3129") == "http://u:p@h.example.net:3129"
    assert n("http://u:p@h.example.net:3129#us1") == "http://u:p@h.example.net:3129#us1"
    assert n("h.example.net:3129:u:p") == "http://u:p@h.example.net:3129"
    assert n("h.example.net:3129") == "http://h.example.net:3129"
    assert n("socks5://u:p@h.example.net:1080") == "socks5://u:p@h.example.net:1080"
    for bad in ("", "# comment", "vless://x@h:443", "nonsense", "h:99999", "a:b:c"):
        assert n(bad) is None


def test_parse_proxy_for_mihomo_and_name_has_no_credentials():
    p = gmc.parse_proxy("u:p%40x@h.example.net:3129")
    assert p == {"name": "px-h.example.net-3129", "type": "http", "server": "h.example.net",
                 "port": 3129, "username": "u", "password": "p@x"}
    assert "u" not in p["name"].split("-")[0:1] and "p@x" not in p["name"]
    assert gmc.parse_proxy("http://h.example.net:1#de5")["name"] == "de5"


def test_load_pool_skips_junk_and_dups_keeps_order():
    d = Path(tempfile.mkdtemp())
    f = d / "all_proxys.txt"
    f.write_text("# hdr\n\nb.example.net:1:u:p\na.example.net:2:u:p\nu:p@b.example.net:1\nssh://x@h:1\n", encoding="utf-8")
    pool = rot.load_pool(f)
    assert [n.name for n in pool] == ["px-b.example.net-1", "px-a.example.net-2"]


def test_pool_mixes_vless_and_proxies_in_file_order():
    d = Path(tempfile.mkdtemp())
    f = d / "all_nodes.reddit_ok.txt"
    f.write_text("http://u:p@a.example.net:1\nvless://00000000-0000-4000-8000-000000000001@v.example.net:443?security=tls#us1\n"
                 "b.example.net:2:u:p\n", encoding="utf-8")
    pool = rot.load_pool(f)
    assert [gmc.link_kind(n.uri) for n in pool] == ["proxy", "vless", "proxy"]
    assert [n.name for n in rot.of_sources(pool, ["vless"])] == ["us1"]
    assert len(rot.of_sources(pool, ["proxy"])) == 2 and len(rot.of_sources(pool, ["vless", "proxy"])) == 3


def test_load_pools_reads_both_files_and_parse_node_dispatches():
    d = Path(tempfile.mkdtemp())
    (d / "all_nodes.txt").write_text("vless://00000000-0000-4000-8000-000000000001@v.example.net:443?security=tls#us1\n", encoding="utf-8")
    (d / "all_proxys.txt").write_text("u:p@a.example.net:1\n", encoding="utf-8")
    pool = rot.load_pools(d / "all_nodes.txt")
    assert [gmc.link_kind(n.uri) for n in pool] == ["vless", "proxy"]
    assert gmc.parse_node(pool[0].uri)["type"] == "vless" and gmc.parse_node(pool[1].uri)["type"] == "http"
    assert rot.pools_exist(d / "all_nodes.txt") and not rot.pools_exist(d / "none.txt", "none2.txt")


def test_pick_nodes_mixes_by_availability():
    import random
    mk = lambda i, uri, delay=None: ags.Free(rot.Node(uri, f"n{i}", uri), f"n{i}", "us", delay)
    prox = [mk(i, f"http://u:p@h{i}.example.net:1", 100 + i) for i in range(4)]
    vl = [mk(10 + i, f"vless://00000000-0000-4000-8000-00000000000{i}@v{i}.example.net:443#us{i}") for i in range(4)]
    got = ags.pick_nodes(prox + vl, 4, random.Random(1))
    assert len(got) == 4 and sum(gmc.link_kind(f.node.uri) == "proxy" for f in got) == 2
    only = ags.pick_nodes(prox, 2, random.Random(1))
    assert [f.mihomo_name for f in only] == ["n0", "n1"]


def test_pick_fastest_by_delay_then_file_rank():
    mk = lambda i, delay=None, rank=None: ags.Free(None, f"n{i}", "??", delay, rank)
    c = [mk(1, 300), mk(2, 100), mk(3, 200), mk(4), mk(5, rank=0)]
    assert [f.mihomo_name for f in ags.pick_fastest(c, 3)] == ["n2", "n3", "n1"]
    # без замеров — по месту в reddit_ok-файле
    c = [mk(1, rank=2), mk(2, rank=0), mk(3, rank=1)]
    assert [f.mihomo_name for f in ags.pick_fastest(c, 2)] == ["n2", "n3"]


def test_geo_from_exit_ip_cache(monkeypatch=None):
    d = Path(tempfile.mkdtemp())
    old = rot.GEO_CACHE_PATH
    rot.GEO_CACHE_PATH = d / "proxy_geo.json"
    rot._geo_cache.update(mtime=None, data={})
    try:
        assert rot.country_for("px-h.example.net-1") == "??"
        rot.GEO_CACHE_PATH.write_text('{"px-h.example.net-1": "DE"}', encoding="utf-8")
        assert rot.country_for("px-h.example.net-1") == "de"
        assert rot.geo_for("px-h.example.net-1")["locale"] == "de-DE"
        assert rot.country_for("us74") == "us"   # код в имени важнее кэша
    finally:
        rot.GEO_CACHE_PATH = old
        rot._geo_cache.update(mtime=None, data={})


def test_resolve_countries_uses_exit_ip_through_proxy():
    d = Path(tempfile.mkdtemp())
    old_root, old_cache = ags.ROOT, rot.GEO_CACHE_PATH
    ags.ROOT = d
    rot.GEO_CACHE_PATH = d / "state" / "proxy_geo.json"
    rot._geo_cache.update(mtime=None, data={})
    seen = []

    def fake_lookup(url, timeout=10.0):
        seen.append(url)
        return {"country": "NL"}

    try:
        node = rot.Node("http://u:p@h.example.net:1", "px-h.example.net-1", "http://u:p@h.example.net:1")
        f = ags.Free(node, node.name, "??")
        assert fn.resolve_countries([f], 2, lookup=fake_lookup) == 1
        assert seen == ["http://u:p@h.example.net:1"] and f.country == "nl"
    finally:
        ags.ROOT, rot.GEO_CACHE_PATH = old_root, old_cache
        rot._geo_cache.update(mtime=None, data={})
