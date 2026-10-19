"""Сквозной тест scripts/add_guest_slots.py на синтетической копии проекта:
фейковый пул нод, фейковый API mihomo (живые/мёртвые/отсутствующие ноды),
заглушка curl_cffi. Реальные файлы проекта не трогаются.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    "chrome99", "chrome107", "chrome116", "chrome119", "chrome120", "chrome123", "chrome124", "chrome131",
    "chrome133a", "chrome136", "chrome142", "chrome145", "chrome146", "chrome150", "chrome131_android",
    "firefox133", "firefox135", "firefox144", "firefox147", "safari170", "safari172_ios", "safari180",
    "safari180_ios", "safari184", "safari184_ios", "safari260", "safari260_ios", "safari2601",
    "edge101", "edge122",
]
# настоящий curl_cffi знает десятки таргетов — для заглушки достаточно набрать с запасом
TARGETS += [f"chrome{n}" for n in range(117, 150) if f"chrome{n}" not in TARGETS]
TARGETS += [f"firefox{n}" for n in range(115, 146) if f"firefox{n}" not in TARGETS]
STUB_CURL = (
    "import enum\n"
    f"BrowserType = enum.Enum('BrowserType', {{n: n for n in {TARGETS!r}}})\n"
    "def get(*a, **k):\n    raise RuntimeError('network disabled in tests')\n"
    "class Session:\n    def __init__(self, *a, **k): pass\n"
)

COUNTRIES = ["us", "de", "nl", "fr", "jp", "sg", "ca", "uk", "pl", "se", "no", "es"]


def vless(i, name):
    return f"http://user{i}:pass{i}@host{i}.example.net:3128#{name}"


def trim_to_25_slots(tmp: Path):
    """В репо уже лежат слоты 26..55. Для сценария «добавить НОВЫЕ слоты» берём
    базу из 25 аккаунтов: вырезаем 26..55 из accounts.yaml, guest yaml и
    rotation.slots."""
    import re

    acc = (tmp / "accounts.yaml").read_text(encoding="utf-8")
    cut = acc.index("  # --- Гостевые слоты (26..55) ---")
    (tmp / "accounts.yaml").write_text(acc[:cut].rstrip("\n") + "\n", encoding="utf-8")

    gp = tmp / "scripts" / "refresh_guest_cookies.yaml"
    text = gp.read_text(encoding="utf-8")
    m = re.search(r"(?m)^guest_accounts:[ \t]*\n", text)
    guest = yaml.safe_load(text)["guest_accounts"]
    guest = {k: v for k, v in guest.items() if int(k.split("_")[1]) <= 25}
    gp.write_text(text[:m.start()] + yaml.safe_dump({"guest_accounts": guest}, allow_unicode=True,
                                                    sort_keys=False, width=200), encoding="utf-8")

    cfg = (tmp / "config.yaml").read_text(encoding="utf-8")
    cfg = re.sub(r"(?m)^    - account_(2[6-9]|[3-5]\d)\s*\n", "", cfg)
    (tmp / "config.yaml").write_text(cfg, encoding="utf-8")


def build_fixture(tmp: Path, mihomo_port: int, trim: bool = True):
    (tmp / "scripts").mkdir()
    for f in ("generate_mihomo_config.py", "rotate_fingerprints.py", "rotation.py", "add_guest_slots.py"):
        shutil.copy2(ROOT / "scripts" / f, tmp / "scripts" / f)
    shutil.copy2(ROOT / "scripts" / "refresh_guest_cookies.yaml", tmp / "scripts" / "refresh_guest_cookies.yaml")
    shutil.copy2(ROOT / "accounts.yaml", tmp / "accounts.yaml")
    shutil.copy2(ROOT / "config.yaml", tmp / "config.yaml")

    if trim:
        trim_to_25_slots(tmp)
    cfg = (tmp / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace('mihomo_api: "http://127.0.0.1:9090"', f'mihomo_api: "http://127.0.0.1:{mihomo_port}"')
    (tmp / "config.yaml").write_text(cfg, encoding="utf-8")

    # назначенные ноды: аккаунты 1..24 (у account_25 ноды нет — как на сервере)
    lines = []
    for n in range(1, 25):
        lines.append(f"account_{n} = {vless(n, 'rs%d' % n if n <= 10 else 'cz%d' % n)}")
    (tmp / "proxy_accounts.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # пул: 41 живая нода (12 стран) + 2 мёртвые + 1 "призрак" (нет в mihomo) + zz1 (неизвестная страна)
    # + rs1 (имя совпадает с назначенной нодой account_1)
    pool = []
    i = 100
    for k in range(41):
        pool.append(vless(i, f"{COUNTRIES[k % len(COUNTRIES)]}{k + 1}"))
        i += 1
    pool.append(vless(i, "us90dead")); i += 1
    pool.append(vless(i, "de91dead")); i += 1
    pool.append(vless(i, "fr92ghost")); i += 1
    pool.append(vless(i, "zz1")); i += 1           # неизвестная страна
    pool.append(vless(i, "rs1")); i += 1           # коллизия имени с назначенной нодой account_1
    (tmp / "all_proxys.txt").write_text("\n".join(pool) + "\n", encoding="utf-8")

    stub = tmp / "stubs" / "curl_cffi"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("from . import requests\n", encoding="utf-8")
    (stub / "requests.py").write_text(STUB_CURL, encoding="utf-8")
    return pool


BANNED: set = set()   # имена нод, которые «живы», но Reddit (expected=200) их не пускает


def delay_of(name: str) -> int:
    """Детерминированная «скорость» фейкового прокси: rs1* — самый быстрый, остальные — по номеру в имени."""
    if name.startswith("rs1"):
        return 50
    import re
    m = re.search(r"(\d+)", name)
    return 100 + int(m.group(1)) if m else 999


class FakeMihomo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if "/delay" in path:
            name = urllib.parse.unquote(path.split("/proxies/", 1)[1].rsplit("/delay", 1)[0])
            if "ghost" in name:
                return self._send(404, {"message": "resource not found"})
            if "dead" in name:
                return self._send(504, {"message": "An error occurred in the delay test"})
            if "expected=200" in self.path and name in BANNED:
                return self._send(504, {"message": "An error occurred in the delay test"})
            return self._send(200, {"delay": delay_of(name)})
        self._send(404, {})

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run(tmp: Path, *args):
    env = {**os.environ, "PYTHONPATH": str(tmp / "stubs"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(tmp / "scripts" / "add_guest_slots.py"), *args],
                          cwd=tmp, env=env, capture_output=True, text=True, timeout=120)


def test_end_to_end():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMihomo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="addslots-"))
    try:
        build_fixture(tmp, srv.server_port)
        before = {f: (tmp / f).read_text(encoding="utf-8")
                  for f in ("accounts.yaml", "config.yaml", "proxy_accounts.env", "scripts/refresh_guest_cookies.yaml")}

        # --- 1) план: ничего не пишет ---
        r = run(tmp, "--count", "30", "--seed", "7")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[план] ничего не записано" in r.stdout
        for f, text in before.items():
            assert (tmp / f).read_text(encoding="utf-8") == text, f"{f} изменён в режиме плана"
        # 46 свободных в пуле: 41 живая + zz1 + rs1(коллизия имени) = 43 живых, 2 мёртвые, 1 призрак
        assert "живых 43" in r.stdout and "не отвечают 2" in r.stdout and "нет в mihomo 1" in r.stdout, r.stdout

        # --- 2) запись ---
        r = run(tmp, "--count", "30", "--seed", "7", "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        assert list(tmp.glob("accounts.yaml.bak-addslots-*")), "нет бэкапа accounts.yaml"

        accounts = yaml.safe_load((tmp / "accounts.yaml").read_text(encoding="utf-8"))["accounts"]
        by = {a["name"]: a for a in accounts}
        assert len(accounts) == 55 and "account_55" in by and "account_56" not in by

        new = [by[f"account_{n}"] for n in range(26, 56)]
        assert all(a["enabled"] is True for a in new)
        ports = [a["proxy_port"] for a in accounts]
        assert len(ports) == len(set(ports)), "дубли портов"
        assert sorted(a["proxy_port"] for a in new) == list(range(7916, 7946))
        assert all(a["cookie_file"] == f"cookies/{a['name']}.json" for a in new)

        # все гостевые слоты 11..55 включены; логин-аккаунты не тронуты
        assert all(by[f"account_{n}"]["enabled"] for n in range(11, 56))
        orig = {a["name"]: a for a in yaml.safe_load(before["accounts.yaml"])["accounts"]}
        for n in range(1, 11):
            assert by[f"account_{n}"] == orig[f"account_{n}"]
        # у account_25 (не было ноды) теперь есть нода и locale под неё
        assert "account_25" in (tmp / "proxy_accounts.env").read_text(encoding="utf-8")

        # --- ноды: уникальные, живые, нужные страны, запас на ротацию ---
        env = (tmp / "proxy_accounts.env").read_text(encoding="utf-8")
        links = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
                 for l in env.splitlines() if l.startswith("account_")}
        assert len(links) == 55
        keys = [v.split("#", 1)[0] for v in links.values()]
        assert len(keys) == len(set(keys)), "одна нода назначена двум слотам"
        added_links = [links[f"account_{n}"] for n in range(25, 56)]
        assert not any("dead" in l or "ghost" in l for l in added_links), "взяты мёртвые/отсутствующие ноды"
        names = [urllib.parse.unquote(v.split("#", 1)[1]) for v in links.values()]
        assert len(names) == len(set(names)), f"имена прокси не уникальны: {names}"
        assert any(n == "rs1~2" for n in names), "коллизия имени rs1 не разрешена"

        pool_keys = {l.split("#", 1)[0] for l in (tmp / "all_proxys.txt").read_text(encoding="utf-8").split()}
        free_left = pool_keys - set(keys)
        # мёртвая пара + призрак + zz1 + (живые на запас >= 8)
        assert len(free_left) >= 8 + 3, f"запас на ротацию съеден: осталось {len(free_left)}"
        # выбраны САМЫЕ БЫСТРЫЕ из живых: любой выданный не медленнее любого оставшегося живого
        def live_delay(uri):
            nm = urllib.parse.unquote(uri.split("#", 1)[1]) if "#" in uri else ""
            return delay_of(nm)
        live_left = [u for u in free_left if "dead" not in u and "ghost" not in u]
        assert max(live_delay(l) for l in added_links) <= min(live_delay(u) for u in live_left), \
            "выданы не самые быстрые прокси"

        # --- гео слота = гео ноды (accounts.yaml:locale и guest spec geo) ---
        guest = yaml.safe_load((tmp / "scripts" / "refresh_guest_cookies.yaml").read_text(encoding="utf-8"))
        assert all(f"account_{n}" in guest["guest_accounts"] for n in range(11, 56))
        sys.path.insert(0, str(tmp / "scripts"))
        sys.modules.pop("rotation", None)
        import importlib
        rot = importlib.import_module("rotation")
        for n in range(25, 56):
            slot = f"account_{n}"
            node_name = urllib.parse.unquote(links[slot].split("#", 1)[1]).split("~")[0]
            geo = rot.geo_for(node_name)
            spec = guest["guest_accounts"][slot]
            assert spec["geo"] == geo, (slot, spec["geo"], geo)
            assert by[slot]["locale"] == geo["locale"], (slot, by[slot]["locale"], geo)
        # у новых слотов — полный профиль браузера
        for n in range(26, 56):
            spec = guest["guest_accounts"][f"account_{n}"]
            assert spec["impersonate"] == by[f"account_{n}"]["impersonate"]
            assert spec["engine"] in ("chromium", "firefox", "webkit") and spec["user_agent"]
        assert "повторится" not in r.stdout, "таргетов хватало, предупреждения о повторах быть не должно"
        new_imps = [by[f"account_{n}"]["impersonate"] for n in range(26, 56)]
        assert len(set(new_imps)) == len(new_imps), "impersonate повторяются, хотя таргетов хватало"
        assert not any(i.startswith("edge") for i in new_imps), "Edge требует msedge — не должен выбираться"

        # --- ротация: новые слоты вписаны, чужой список не испорчен ---
        cfg = yaml.safe_load((tmp / "config.yaml").read_text(encoding="utf-8"))
        slots = cfg["rotation"]["slots"]
        assert all(f"account_{n}" in slots for n in range(11, 56))
        assert len(slots) == len(set(slots)), "дубли в rotation.slots"
        # остальной config.yaml не пострадал
        old_cfg = yaml.safe_load(before["config.yaml"])
        old_cfg["rotation"]["slots"] = slots
        assert cfg == old_cfg

        # --- итоговый конфиг mihomo собирается: 55 слушателей, имена прокси уникальны ---
        env2 = {**os.environ, "PYTHONPATH": str(tmp / "stubs")}
        g = subprocess.run([sys.executable, str(tmp / "scripts" / "generate_mihomo_config.py")],
                           cwd=tmp, env=env2, capture_output=True, text=True, timeout=60)
        assert g.returncode == 0, g.stdout + g.stderr
        mh = yaml.safe_load((tmp / "mihomo_config.yaml").read_text(encoding="utf-8"))
        assert len(mh["listeners"]) == 55
        pn = [p["name"] for p in mh["proxies"]]
        assert len(pn) == len(set(pn)), "дубли имён прокси в mihomo_config.yaml"

        # --- 3) повторный запуск не плодит слоты ---
        r2 = run(tmp, "--count", "0", "--apply")
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert len(yaml.safe_load((tmp / "accounts.yaml").read_text(encoding="utf-8"))["accounts"]) == 55

        # --- 4) запас: просим больше, чем есть — берём не больше доступного ---
        r3 = run(tmp, "--count", "100", "--seed", "1")
        assert r3.returncode == 0 and "ВНИМАНИЕ: нужно" in r3.stdout, r3.stdout + r3.stderr
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_patch_helpers():
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    ags = importlib.import_module("add_guest_slots")

    text = ("accounts:\n  - name: account_5\n    enabled: false\n    proxy_port: 7001\n    impersonate: x\n"
            "  - name: account_6\n    enabled: false\n    proxy_port: 7002\n    locale: en-US\n")
    out = ags.patch_account_fields(text, "account_5", enabled=True, locale="ja-JP")
    d = {a["name"]: a for a in yaml.safe_load(out)["accounts"]}
    assert d["account_5"]["enabled"] is True and d["account_5"]["locale"] == "ja-JP"
    assert d["account_6"]["enabled"] is False and d["account_6"]["locale"] == "en-US"   # соседний блок цел
    out2 = ags.patch_account_fields(out, "account_6", locale="de-DE")
    assert {a["name"]: a for a in yaml.safe_load(out2)["accounts"]}["account_6"]["locale"] == "de-DE"

    cfg = "rotation:\n  enabled: true\n  slots:\n    - account_11\n    - account_12\n\n  empty_streak_trigger: 6\n"
    new, left = ags.patch_config_slots(cfg, ["account_12", "account_26"])
    assert left == [] and yaml.safe_load(new)["rotation"]["slots"] == ["account_11", "account_12", "account_26"]
    _, left = ags.patch_config_slots("rotation:\n  slots: [a, b]\n", ["account_26"])
    assert left == ["account_26"]                      # flow-стиль не трогаем — просим вписать вручную

    assert ags.allocate_ports({7915, 7892}, 3, check=lambda p: p != 7917) == [7916, 7918, 7919]


def test_pick_profiles_reuses_only_when_targets_run_out():
    import importlib
    import random
    import types

    stub = types.ModuleType("curl_cffi")
    req = types.ModuleType("curl_cffi.requests")
    import enum
    req.BrowserType = enum.Enum("BrowserType", {n: n for n in ["chrome131", "chrome136", "firefox135"]})
    stub.requests = req
    saved = {k: sys.modules.get(k) for k in ("curl_cffi", "curl_cffi.requests")}
    sys.modules["curl_cffi"], sys.modules["curl_cffi.requests"] = stub, req
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        ags = importlib.import_module("add_guest_slots")
        fp = types.SimpleNamespace(allow_old=False, no_edge=True, no_mobile=False)
        profs, reused = ags.pick_profiles(2, {"chrome131"}, random.Random(1), fp)
        assert not reused and {p.impersonate for p in profs} == {"chrome136", "firefox135"}
        profs, reused = ags.pick_profiles(5, set(), random.Random(1), fp)   # 3 таргета на 5 слотов
        assert reused and len(profs) == 5 and len({p.impersonate for p in profs}) == 3
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_fresh_server_bootstrap():
    """Новый сервер: в репо уже 55 слотов, есть только пул нод (без
    proxy_accounts.env). generate_mihomo_config поднимает конфиг из одного
    пула, а add_guest_slots --count 0 раздаёт ноды готовым слотам 11..55."""
    import importlib

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMihomo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="freshserver-"))
    try:
        build_fixture(tmp, srv.server_port, trim=False)
        (tmp / "proxy_accounts.env").unlink()                       # чистый сервер
        pool = [vless(200 + k, f"{COUNTRIES[k % len(COUNTRIES)]}{k + 1}") for k in range(60)]
        (tmp / "all_proxys.txt").write_text("\n".join(pool) + "\n", encoding="utf-8")

        env = {**os.environ, "PYTHONPATH": str(tmp / "stubs"), "PYTHONDONTWRITEBYTECODE": "1"}
        gen = lambda: subprocess.run([sys.executable, str(tmp / "scripts" / "generate_mihomo_config.py")],
                                     cwd=tmp, env=env, capture_output=True, text=True, timeout=60)

        # 1) mihomo из одного пула (без назначенных нод) — раньше тут была ошибка
        g = gen()
        assert g.returncode == 0, g.stdout + g.stderr
        mh = yaml.safe_load((tmp / "mihomo_config.yaml").read_text(encoding="utf-8"))
        assert len(mh["proxies"]) == 60 and mh["listeners"] == []

        # 2) ноды раздаются ГОТОВЫМ слотам 11..55 (новые слоты не создаются)
        r = run(tmp, "--count", "0", "--seed", "3", "--apply", "--disable-login-without-node")
        assert r.returncode == 0, r.stdout + r.stderr
        accounts = yaml.safe_load((tmp / "accounts.yaml").read_text(encoding="utf-8"))["accounts"]
        assert len(accounts) == 55, "новых слотов быть не должно"
        # логин-аккаунты без ноды выключены, гостевые включены
        assert not any(a["enabled"] for a in accounts if int(a["name"].split("_")[1]) <= 10)
        links = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
                 for l in (tmp / "proxy_accounts.env").read_text(encoding="utf-8").splitlines()
                 if l.startswith("account_")}
        assert sorted(links, key=lambda k: int(k.split("_")[1])) == [f"account_{n}" for n in range(11, 56)]
        assert len({v.split("#", 1)[0] for v in links.values()}) == 45
        assert 60 - 45 >= 8, "запас на ротацию должен сохраниться"

        # 3) гео слота = страна ноды (accounts.yaml:locale и guest spec geo)
        sys.path.insert(0, str(tmp / "scripts"))
        sys.modules.pop("rotation", None)
        rot = importlib.import_module("rotation")
        guest = yaml.safe_load((tmp / "scripts" / "refresh_guest_cookies.yaml").read_text(encoding="utf-8"))
        by = {a["name"]: a for a in accounts}
        for n in range(11, 56):
            slot = f"account_{n}"
            geo = rot.geo_for(urllib.parse.unquote(links[slot].split("#", 1)[1]))
            assert guest["guest_accounts"][slot]["geo"] == geo, slot
            assert by[slot]["locale"] == geo["locale"], slot
            assert by[slot]["enabled"] is True
        # у слотов 11..25 в account_geo не осталось устаревшего «планового» гео
        for n in range(11, 26):
            planned = guest["account_geo"][f"account_{n}"]
            assert planned == guest["guest_accounts"][f"account_{n}"]["geo"], f"account_{n}: account_geo не обновлён"

        # 4) теперь mihomo получает 45 слушателей
        g = gen()
        assert g.returncode == 0, g.stdout + g.stderr
        mh = yaml.safe_load((tmp / "mihomo_config.yaml").read_text(encoding="utf-8"))
        assert len(mh["listeners"]) == 45
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_replace_banned():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMihomo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="addslots-"))
    try:
        build_fixture(tmp, srv.server_port)

        def links():
            return {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
                    for l in (tmp / "proxy_accounts.env").read_text(encoding="utf-8").splitlines()
                    if l.startswith("account_")}

        before = links()
        old = before["account_12"]
        BANNED.add(urllib.parse.unquote(old.split("#", 1)[1]))
        try:
            r = run(tmp, "--count", "0", "--replace-banned", "--spare", "2", "--seed", "3", "--apply")
        finally:
            BANNED.clear()
        assert r.returncode == 0, r.stdout + r.stderr
        assert "снимаю ноду у 1: account_12" in r.stdout, r.stdout
        after = links()
        assert after["account_12"] != old
        assert all(after[k] == v for k, v in before.items() if k not in ("account_12", "account_25")), "тронуты чужие слоты"
        env = (tmp / "proxy_accounts.env").read_text(encoding="utf-8")
        assert "# (снято: Reddit не пускает) account_12" in env
        # старая забаненная нода не выдана никому
        assert old.split("#", 1)[0] not in [v.split("#", 1)[0] for v in after.values()]
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
