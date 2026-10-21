"""Тесты scripts/attach_proxies.py: чистые функции + сквозной сценарий на временной копии проекта.
Сеть не используется: замер прокси и определение гео подменяются, реальные файлы проекта не трогаются."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import attach_proxies as ap  # noqa: E402
import rotation as rot  # noqa: E402
from scraper import profile_gen as pg  # noqa: E402

ACCOUNTS = """\
accounts:
  # комментарий должен сохраниться
  - name: account_1
    enabled: true
    cookie_file: cookies/account_1.json
    proxy_port: 7892
    locale: en-US
    impersonate: chrome131
    user_agent: "UA1"

  - name: account_11
    enabled: false
    cookie_file: cookies/account_11.json
    proxy_port: 7900
    locale: en-US
    impersonate: chrome120
    user_agent: "UA11"

  - name: account_12
    enabled: false
    cookie_file: cookies/account_12.json
    proxy_port: 7901
    locale: en-US
    impersonate: chrome119
    user_agent: "UA12"
"""

GUEST = """\
# шапка
guest_accounts:
  account_11:
    engine: chromium
    channel: null
    user_agent: UA11
    impersonate: chrome120
  account_12:
    engine: chromium
    channel: null
    user_agent: UA12
    impersonate: chrome119
"""

CONFIG = """\
rotation:
  enabled: true
  slots:
    - account_11
    - account_12
"""

OLD_PROXY = "http://olduser:oldpass@10.0.0.1:3128"
PROXIES = {   # хост -> (строка в all_proxies.txt, задержка мс | None=негоден)
    "10.1.0.1": ("u:p@10.1.0.1:3129#us1", 120),
    "10.1.0.2": ("u:p@10.1.0.2:3129#us2", 50),
    "10.1.0.3": ("u:p@10.1.0.3:3129#de1", 300),
    "10.1.0.4": ("u:p@10.1.0.4:3129#us3", None),
}


def make_project(tmp_path, monkeypatch, extra_proxies=()):
    (tmp_path / "state").mkdir()
    files = {"accounts.yaml": ACCOUNTS, "config.yaml": CONFIG,
             "vless_accounts.env": f"account_11 = {OLD_PROXY}\n",
             "all_proxies.txt": "\n".join([v[0] for v in PROXIES.values()] + list(extra_proxies)) + "\n"}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "guest.yaml").write_text(GUEST, encoding="utf-8")
    st = tmp_path / "state"
    for attr, val in {"ROOT": tmp_path, "ACCOUNTS_PATH": tmp_path / "accounts.yaml",
                      "CONFIG_PATH": tmp_path / "config.yaml", "ENV_PATH": tmp_path / "vless_accounts.env",
                      "GUEST_PATH": tmp_path / "guest.yaml", "STATE_DIR": st, "IDENT_PATH": st / "identities.json",
                      "PA_STATE_PATH": st / "proxy_attach.json", "GEO_CACHE_PATH": st / "proxy_geo.json",
                      "HUB_STATUS_PATH": st / "login_hub" / "status.json",
                      "LOGIN_PATH": tmp_path / "login.yaml", "STORAGE_DIR": tmp_path / "storage_state"}.items():
        monkeypatch.setattr(ap, attr, val)
    monkeypatch.setattr(rot, "STATE_FILE", st / "rotation_state.json")
    monkeypatch.setattr(rot, "STATE_DIR", st)

    def fake_probe(uri, url=None, timeout=8.0):
        host = rot.node_key(uri).rsplit("@", 1)[-1].split(":")[0]
        ms = PROXIES.get(host, (None, 200))[1]
        return (ms, None) if ms is not None else (None, "HTTP 403")

    monkeypatch.setattr(ap, "probe_proxy", fake_probe)
    return tmp_path


def run(*argv, mode="guest"):
    return ap.main([*argv, "--no-geo", "--seed", "7", "--mode", mode])


LOGIN_ACCOUNTS = """\
accounts:
  - name: account_1
    enabled: true
    login: true
    cookie_file: cookies/account_1.json
    storage_state: storage_state/account_1.json
    proxy_port: 7892
    locale: en-US
    impersonate: chrome131
    user_agent: "UA1"

  - name: account_2
    enabled: false
    login: true
    cookie_file: cookies/account_2.json
    storage_state: storage_state/account_2.json
    proxy_port: 7893
    locale: en-US
    impersonate: chrome119
    user_agent: "UA2"
"""

LOGIN_YAML = """\
login_accounts:
  - account_1
  - account_2

account_geo:
  account_1: {locale: "en-US", timezone_id: "America/New_York"}
  account_2: {locale: "en-US", timezone_id: "America/New_York"}
"""


def make_login_project(tmp_path, monkeypatch):
    """Парк, где ВСЕ аккаунты логин-слоты (гостевых нет, refresh_guest_cookies.yaml не существует)."""
    make_project(tmp_path, monkeypatch)
    (tmp_path / "accounts.yaml").write_text(LOGIN_ACCOUNTS, encoding="utf-8")
    (tmp_path / "login.yaml").write_text(LOGIN_YAML, encoding="utf-8")
    (tmp_path / "guest.yaml").unlink()
    (tmp_path / "vless_accounts.env").write_text(f"account_1 = {OLD_PROXY}\n", encoding="utf-8")
    return tmp_path


def test_judge_response():
    assert ap.judge_response(200, 500_000, "<html>recaptcha script</html>", 80) == (80, None)
    assert ap.judge_response(200, 900, "Please verify you are human", 80) == (None, "капча/блокировка")
    assert ap.judge_response(403, 900, "blocked", 80) == (None, "HTTP 403")
    assert ap.judge_response(429, 10, "", 80) == (None, "HTTP 429")


def test_rank_nodes_fast_first_unmeasured_last():
    mk = lambda name, d: SimpleNamespace(mihomo_name=name, delay=d)  # noqa: E731
    out = ap.rank_nodes([mk("slow", 300), mk("none", None), mk("fast", 40), mk("mid", 120)])
    assert [f.mihomo_name for f in out] == ["fast", "mid", "slow", "none"]


def test_active_bad_keys_respects_ttl():
    now = 1_000_000.0
    st = {"bad": {"fresh": {"ts": now - 3600, "ttl_hours": 72}, "old": {"ts": now - 100 * 3600, "ttl_hours": 72}}}
    assert ap.active_bad_keys(st, now) == {"fresh"}


def test_patch_account_keys_replaces_and_inserts():
    out = ap.patch_account_keys(ACCOUNTS, "account_11", {"enabled": True, "impersonate": "firefox147",
                                                         "user_agent": 'Mozilla "X"', "fonts": "a"})
    data = {a["name"]: a for a in yaml.safe_load(out)["accounts"]}
    assert data["account_11"]["enabled"] is True and data["account_11"]["impersonate"] == "firefox147"
    assert data["account_11"]["user_agent"] == 'Mozilla "X"' and data["account_11"]["fonts"] == "a"
    assert data["account_12"]["enabled"] is False and data["account_1"]["impersonate"] == "chrome131"  # соседи целы
    assert "# комментарий должен сохраниться" in out


def test_set_env_links_replaces_and_appends():
    text = "# c\naccount_1 = old\n# account_2 = commented\n"
    out = ap.set_env_links(text, {"account_1": "new1", "account_2": "new2"}, "STAMP")
    lines = out.splitlines()
    assert "account_1 = new1" in lines and "account_1 = old" not in lines
    assert "# account_2 = commented" in lines and "account_2 = new2" in lines
    assert ap.set_env_links("", {"account_5": "x"}, "S") == "account_5 = x\n"


def test_parse_accounts():
    known = [f"account_{n}" for n in range(1, 20)]
    assert ap.parse_accounts(["11", "account_13", "15-17", "11,12"], known) == [
        "account_11", "account_13", "account_15", "account_16", "account_17", "account_12"]
    try:
        ap.parse_accounts(["99"], known)
    except SystemExit as e:
        assert "account_99" in str(e)
    else:
        raise AssertionError("ждали SystemExit для неизвестного аккаунта")


def test_hub_captcha_accounts(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"sessions": [
        {"account": "account_11", "status": "waiting_user", "message": "captcha"},
        {"account": "account_12", "status": "waiting_user", "message": "email"},
        {"account": "account_13", "status": "waiting_login", "message": ""}]}), encoding="utf-8")
    assert ap.hub_captcha_accounts(p) == ["account_11"]
    assert ap.hub_captcha_accounts(tmp_path / "нет.json") == []


def test_geo_for_node_exit_ip_then_name_fallback(monkeypatch):
    f = SimpleNamespace(mihomo_name="de1", node=SimpleNamespace(uri="http://u:p@1.2.3.4:80#de1"))
    monkeypatch.setattr(ap.geoip, "resolve_geo", lambda url, fetch=None: {"country": "NL", "locale": "nl-NL",
                                                                          "timezone_id": "Europe/Amsterdam"})
    geo, warn = ap.geo_for_node(f)
    assert geo["country"] == "NL" and warn is None

    def boom(url, fetch=None):
        raise RuntimeError("secret u:p@1.2.3.4")

    monkeypatch.setattr(ap.geoip, "resolve_geo", boom)
    geo, warn = ap.geo_for_node(f)
    assert geo["country"] == "DE" and geo["locale"] == "de-DE" and geo["geo_source"] == "node-name"
    assert "RuntimeError" in warn and "secret" not in warn          # текст исключения (адрес/логин) не утекает
    geo, warn = ap.geo_for_node(SimpleNamespace(mihomo_name="px-1.2.3.4-80", node=f.node), live=False)
    assert geo["locale"] == "en-US" and geo["geo_note"] and warn


def test_fresh_profile_avoids_impersonate_and_taken():
    presets = pg.load_presets()
    geo = ap._fill_geo_meta(ap.geoip.geo_for_country("US"), "test")
    taken: set[str] = set()
    first = ap.fresh_profile("account_20", geo, presets, taken, set(), 1)
    second = ap.fresh_profile("account_20", geo, presets, taken, {first["impersonate"]}, 2)
    assert pg.fingerprint(first) != pg.fingerprint(second)
    assert second["impersonate"] != first["impersonate"]
    assert len(taken) == 2 and not pg.validate_profile(second, presets)


def test_add_plan_does_not_write(tmp_path, monkeypatch):
    make_project(tmp_path, monkeypatch)
    before = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file()}
    assert run("add", "--count", "1") == 0
    assert {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file()} == before
    assert not (tmp_path / "state" / "profiles").exists()


def test_add_assigns_fastest_first_and_writes_everything(tmp_path, monkeypatch):
    make_project(tmp_path, monkeypatch)
    assert run("add", "--count", "1", "--apply") == 0
    env = {k.strip(): v.strip() for k, v in (ln.split("=", 1) for ln in
                                              (tmp_path / "vless_accounts.env").read_text().splitlines()
                                              if ln.startswith("account_"))}
    # account_12 — существующий гостевой слот без узла: получает САМЫЙ быстрый (50 мс); новый — следующий (120 мс)
    assert "10.1.0.2" in env["account_12"] and "10.1.0.1" in env["account_13"]
    assert env["account_11"] == OLD_PROXY                              # занятый слот не тронут
    assert not any("10.1.0.3" in v or "10.1.0.4" in v for v in env.values())   # медленный — в запасе, негодный — никогда

    accounts = {a["name"]: a for a in yaml.safe_load((tmp_path / "accounts.yaml").read_text())["accounts"]}
    assert accounts["account_13"]["enabled"] is True and accounts["account_13"]["proxy_port"] >= 7916
    assert accounts["account_12"]["enabled"] is True and accounts["account_12"]["locale"] == "en-US"
    prof = {n: pg.load_profile(n, tmp_path / "state") for n in ("account_12", "account_13")}
    assert all(prof.values())
    for n, p in prof.items():                                          # accounts.yaml согласован с профилем устройства
        assert accounts[n]["impersonate"] == p["impersonate"] and accounts[n]["user_agent"] == p["user_agent"]
    guest = yaml.safe_load((tmp_path / "guest.yaml").read_text())["guest_accounts"]
    assert guest["account_13"]["impersonate"] == prof["account_13"]["impersonate"]
    assert guest["account_13"]["geo"]["locale"] == "en-US" and "account_11" in guest
    assert "- account_13" in (tmp_path / "config.yaml").read_text()
    assert list(tmp_path.glob("*.bak-attach-*"))                       # бэкапы
    hist = json.loads((tmp_path / "state" / "proxy_attach.json").read_text())["history"]
    assert {h["account"] for h in hist} == {"account_12", "account_13"}


def test_replace_swaps_proxy_and_profile_and_blacklists_old(tmp_path, monkeypatch):
    make_project(tmp_path, monkeypatch)
    assert run("add", "--count", "1", "--apply") == 0
    state = tmp_path / "state"
    old_prof = pg.load_profile("account_13", state)
    old_env = (tmp_path / "vless_accounts.env").read_text()
    PROXIES["10.1.0.5"] = ("u:p@10.1.0.5:3129#us5", 200)               # появился новый прокси
    with open(tmp_path / "all_proxies.txt", "a", encoding="utf-8") as fh:
        fh.write(PROXIES["10.1.0.5"][0] + "\n")
    try:
        assert run("replace", "--accounts", "13", "--reason", "капча", "--apply") == 0
    finally:
        PROXIES.pop("10.1.0.5")
    env = (tmp_path / "vless_accounts.env").read_text()
    line13 = next(ln for ln in env.splitlines() if ln.startswith("account_13"))
    assert "10.1.0.5" in line13 and "10.1.0.1" not in line13           # следующий по скорости свободный (200 < 300)
    assert "10.1.0.1" in old_env and "10.1.0.1" not in env              # старый прокси снят
    new_prof = pg.load_profile("account_13", state)
    assert new_prof["impersonate"] != old_prof["impersonate"] and pg.fingerprint(new_prof) != pg.fingerprint(old_prof)
    assert list((state / "profiles" / ".history").glob("account_13.*.json"))   # старый профиль сохранён
    accounts = {a["name"]: a for a in yaml.safe_load((tmp_path / "accounts.yaml").read_text())["accounts"]}
    assert accounts["account_13"]["impersonate"] == new_prof["impersonate"]
    bad = json.loads((state / "proxy_attach.json").read_text())["bad"]
    assert len(bad) == 1 and next(iter(bad.values()))["reason"] == "капча"
    # чёрный список действует: второй раз берётся следующий по скорости (de1, 300 мс), а не уже снятые us1/us5
    assert run("replace", "--accounts", "13", "--apply") == 0
    line13 = next(ln for ln in (tmp_path / "vless_accounts.env").read_text().splitlines()
                  if ln.startswith("account_13"))
    assert "10.1.0.3" in line13 and "10.1.0.1" not in line13 and "10.1.0.5" not in line13
    assert len(json.loads((state / "proxy_attach.json").read_text())["bad"]) == 2


def test_replace_rejects_login_accounts(tmp_path, monkeypatch):
    make_project(tmp_path, monkeypatch)
    try:
        run("replace", "--accounts", "1", "--apply")
    except SystemExit as e:
        assert "не аккаунты режима guest" in str(e)
    else:
        raise AssertionError("логин-аккаунт заменять нельзя")


def test_replace_from_hub_uses_captcha_status(tmp_path, monkeypatch):
    make_project(tmp_path, monkeypatch)
    hub = tmp_path / "state" / "login_hub"
    hub.mkdir()
    (hub / "status.json").write_text(json.dumps({"sessions": [
        {"account": "account_11", "status": "waiting_user", "message": "captcha"}]}), encoding="utf-8")
    assert run("replace", "--from-hub", "--apply") == 0
    env = (tmp_path / "vless_accounts.env").read_text()
    assert OLD_PROXY.split("@")[1] not in env and "10.1.0.2" in env     # самый быстрый свободный
    assert time.time() - json.loads((tmp_path / "state" / "proxy_attach.json").read_text())["bad"][
        rot.node_key(OLD_PROXY)]["ts"] < 60


def test_login_add_creates_login_slots_disabled(tmp_path, monkeypatch):
    make_login_project(tmp_path, monkeypatch)
    assert run("add", "--count", "1", "--apply", mode="login") == 0
    accounts = {a["name"]: a for a in yaml.safe_load((tmp_path / "accounts.yaml").read_text())["accounts"]}
    env = (tmp_path / "vless_accounts.env").read_text()
    # account_2 — логин-слот без узла: получил самый быстрый; account_3 — новый, следующий
    assert "10.1.0.2" in next(ln for ln in env.splitlines() if ln.startswith("account_2"))
    assert "10.1.0.1" in next(ln for ln in env.splitlines() if ln.startswith("account_3"))
    new = accounts["account_3"]
    assert new["login"] is True and new["enabled"] is False            # включится только после входа
    assert new["storage_state"] == "storage_state/account_3.json" and new["proxy_port"] >= 7916
    assert accounts["account_2"]["enabled"] is False and accounts["account_1"]["enabled"] is True
    login = yaml.safe_load((tmp_path / "login.yaml").read_text())
    assert "account_3" in login["login_accounts"] and login["account_geo"]["account_3"]["locale"] == "en-US"
    assert pg.load_profile("account_3", tmp_path / "state") and pg.load_profile("account_2", tmp_path / "state")
    assert not (tmp_path / "guest.yaml").exists()                      # гостевые файлы не создаются
    assert "rotation" in (tmp_path / "config.yaml").read_text() and "account_3" not in (tmp_path / "config.yaml").read_text()


def test_login_replace_skips_accounts_with_session_unless_forced(tmp_path, monkeypatch):
    make_login_project(tmp_path, monkeypatch)
    assert run("add", "--count", "1", "--apply", mode="login") == 0
    (tmp_path / "storage_state").mkdir()
    (tmp_path / "storage_state" / "account_3.json").write_text("{}", encoding="utf-8")
    before = (tmp_path / "vless_accounts.env").read_text()
    assert run("replace", "--accounts", "3", "--apply", mode="login") == 0
    assert (tmp_path / "vless_accounts.env").read_text() == before     # сессию не сбрасываем без --force
    assert run("replace", "--accounts", "3", "--force", "--apply", mode="login") == 0
    assert (tmp_path / "vless_accounts.env").read_text() != before
    PROXIES["10.1.0.6"] = ("u:p@10.1.0.6:3129#us6", 400)               # свободные прокси к этому моменту кончились
    with open(tmp_path / "all_proxies.txt", "a", encoding="utf-8") as fh:
        fh.write(PROXIES["10.1.0.6"][0] + "\n")
    try:
        assert run("replace", "--accounts", "2", "--apply", mode="login") == 0   # без сессии — можно без --force
    finally:
        PROXIES.pop("10.1.0.6")
    assert "10.1.0.6" in next(ln for ln in (tmp_path / "vless_accounts.env").read_text().splitlines()
                              if ln.startswith("account_2"))


def test_failed_render_writes_nothing(tmp_path, monkeypatch):
    """Регрессия: в refresh_guest_cookies.yaml нет секции guest_accounts: — раньше env и accounts.yaml
    успевали измениться до ошибки. Теперь всё формируется и проверяется до записи."""
    make_project(tmp_path, monkeypatch)
    (tmp_path / "guest.yaml").write_text("# пусто\nlogin_accounts: []\n", encoding="utf-8")
    before = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file()}
    try:
        run("add", "--count", "2", "--apply")
    except SystemExit as e:
        assert "guest_accounts" in str(e)
    else:
        raise AssertionError("ждали SystemExit")
    after = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file()}
    assert after == before and not (tmp_path / "state" / "profiles").exists()


def test_pick_with_geo_skips_nodes_without_country(monkeypatch):
    mk = lambda h: SimpleNamespace(mihomo_name=f"px-{h}-80", node=SimpleNamespace(uri=f"http://u:p@{h}:80"))  # noqa: E731
    nodes = [mk("h1"), mk("h2"), mk("h3")]
    us = {"country": "US", "locale": "en-US", "timezone_id": "America/New_York"}

    def fake(url, fetch=None):
        if "h1" in url:
            raise RuntimeError("нет ответа")
        return dict(us)

    monkeypatch.setattr(ap.geoip, "resolve_geo", fake)
    chosen, geos = ap.pick_with_geo(nodes, 2, True)
    assert [f.mihomo_name for f in chosen] == ["px-h2-80", "px-h3-80"]      # h1 без страны пропущен, взят следующий
    assert all(g["country"] == "US" for g, _ in geos)
    none, _ = ap.pick_with_geo(nodes[:1], 1, True)
    assert none == []


def test_geo_for_node_retries_once(monkeypatch):
    calls = []

    def flaky(url, fetch=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой")
        return {"country": "DE", "locale": "de-DE", "timezone_id": "Europe/Berlin"}

    monkeypatch.setattr(ap.geoip, "resolve_geo", flaky)
    f = SimpleNamespace(mihomo_name="px-1-80", node=SimpleNamespace(uri="http://u:p@1.2.3.4:80"))
    geo, warn = ap.geo_for_node(f)
    assert geo["country"] == "DE" and warn is None and len(calls) == 2
