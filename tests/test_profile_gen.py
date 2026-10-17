"""Модуль 03 — постоянный профиль устройства и гео из IP. Без сети и без браузера:
GeoIP подменён заглушкой, запись идёт во временный state/."""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scraper import geoip, profile_gen as pg  # noqa: E402
from scraper.http_client import CurlSessionHandle  # noqa: E402

import profile as profile_cli  # noqa: E402  (scripts/profile.py)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="profile03-"))


def _geo(cc="US"):
    g = geoip.geo_for_country(cc)
    g.update(geo_source="test", exit_ip="203.0.113.7", checked_at=1_700_000_000)
    return g


def _profile(os_name="windows", impersonate=None, seed=1, cc="DE"):
    return pg.generate_profile("account_1", seed, geo=_geo(cc), os_name=os_name,
                               impersonate=impersonate, created_at=1_700_000_000)


# ---------------------------------------------------------------- #
#  Генератор
# ---------------------------------------------------------------- #

def test_fleet_of_64_unique_and_valid():
    names = [f"account_{i}" for i in range(1, 65)]
    ccs = list(geoip.COUNTRY_GEO)
    geos = {n: _geo(ccs[i % len(ccs)]) for i, n in enumerate(names)}
    fleet = pg.generate_fleet(names, base_seed=7, geos=geos)
    assert len(fleet) == 64
    assert len({pg.fingerprint(p) for p in fleet.values()}) == 64          # нет двух одинаковых устройств
    for n, p in fleet.items():
        assert pg.validate_profile(p) == [], n


def test_fleet_is_deterministic_and_diverse():
    names = [f"account_{i}" for i in range(1, 65)]
    a = pg.generate_fleet(names, 123, created_at=1)
    b = pg.generate_fleet(names, 123, created_at=1)
    assert a == b
    assert a != pg.generate_fleet(names, 124, created_at=1)
    oses = {p["os"] for p in a.values()}
    assert {"windows", "macos"} <= oses and len(oses) >= 3                 # парк не однообразный
    assert sum(p["os"] == "windows" for p in a.values()) > 64 * 0.35       # но реалистичный: Windows доминирует
    assert len({p["impersonate"] for p in a.values()}) >= 10


def test_same_seed_same_profile():
    assert _profile(seed=5) == _profile(seed=5)
    assert _profile(seed=5) != _profile(seed=6)


@pytest.mark.parametrize("os_name", ["windows", "macos", "linux", "android", "ios"])
def test_every_platform_generates_valid_profiles(os_name):
    for seed in range(40):
        p = _profile(os_name=os_name, seed=seed)
        assert p["os"] == os_name
        assert pg.validate_profile(p) == [], (os_name, seed, p["impersonate"])


def test_presets_ranges_from_spec():
    ps = [_profile(os_name="windows", seed=s) for s in range(200)]
    assert {(p["screen"]["w"], p["screen"]["h"]) for p in ps} >= {(1366, 768), (1536, 864), (1920, 1080), (2560, 1440)}
    assert all(4 <= p["hardware_concurrency"] <= 16 for p in ps)
    assert all(4 <= p["ram_gb"] <= 32 for p in ps)
    assert all(p["device_memory_gb"] in (4, 8) or p["engine"] != "chromium" for p in ps)   # Chrome режет до 8


def test_chromium_only_fields():
    ff = _profile(impersonate="firefox147")
    assert ff["engine"] == "firefox"
    assert ff["sec_ch_ua"] is ff["sec_ch_ua_mobile"] is ff["sec_ch_ua_platform"] is None
    assert ff["device_memory_gb"] is None
    ch = _profile(impersonate="chrome131")
    assert ch["engine"] == "chromium" and 'v="131"' in ch["sec_ch_ua"] and ch["sec_ch_ua_mobile"] == "?0"
    an = _profile(os_name="android", impersonate="chrome131_android")
    assert an["sec_ch_ua_mobile"] == "?1" and an["sec_ch_ua_platform"] == '"Android"'
    assert "Mobile Safari" in an["user_agent"] and an["screen"]["w"] <= 500
    assert _profile(os_name="ios", impersonate="safari184_ios")["user_agent"].count("iPhone OS 18_4") == 1


def test_sec_ch_ua_matches_real_chrome_samples():
    assert pg.build_sec_ch_ua(120) == '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
    assert pg.build_sec_ch_ua(131) == '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
    assert pg.build_sec_ch_ua(136) == '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'
    assert pg.build_sec_ch_ua(142) == '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'


# ---------------------------------------------------------------- #
#  validate_profile: несогласованное отлавливается
# ---------------------------------------------------------------- #

def _broken(mutator, **kw):
    p = copy.deepcopy(_profile(**kw))
    mutator(p)
    return pg.validate_profile(p)


def test_validate_catches_inconsistencies():
    win = dict(os_name="windows", impersonate="chrome131")
    assert _broken(lambda p: p.update(webgl_renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
                                      webgl_vendor="Google Inc. (Apple)"), **win)       # Windows + Apple GPU
    assert _broken(lambda p: p.update(user_agent=p["user_agent"].replace("Chrome/131", "Chrome/124")), **win)  # UA != TLS
    assert _broken(lambda p: p.update(user_agent=p["user_agent"].replace("Windows NT 10.0; Win64; x64", "Macintosh; Intel Mac OS X 10_15_7")), **win)
    assert _broken(lambda p: p.update(platform="MacIntel"), **win)
    assert _broken(lambda p: p.update(screen={"w": 390, "h": 844}, viewport={"w": 390, "h": 700}), **win)  # моб. экран на десктопе
    assert _broken(lambda p: p.update(device_memory_gb=16), **win)                       # Chrome не отдаёт >8
    assert _broken(lambda p: p.update(device_memory_gb=8), os_name="windows", impersonate="firefox147")
    assert _broken(lambda p: p.update(sec_ch_ua='"Chromium";v="147"'), os_name="windows", impersonate="firefox147")
    assert _broken(lambda p: p.update(sec_ch_ua_platform='"macOS"'), **win)
    assert _broken(lambda p: p.update(engine="firefox"), **win)                          # engine != TLS-семейство
    assert _broken(lambda p: p.update(accept_language="en-US,en;q=0.9"), **win)           # язык != locale
    assert _broken(lambda p: p.update(timezone_id="Mars/Base"), **win)
    assert _broken(lambda p: p.update(viewport={"w": 5000, "h": 5000}), **win)
    assert _broken(lambda p: p.update(device_pixel_ratio=1), os_name="android", impersonate="chrome131_android")
    assert _broken(lambda p: p.pop("user_agent"), **win)
    assert pg.validate_profile(_profile(**win)) == []


def test_unknown_country_is_en_us_with_visible_note():
    g = geoip.geo_for_country("ZZ")
    assert g["locale"] == "en-US" and g["geo_known"] is False and "unknown_country:ZZ" in g["geo_note"]
    p = pg.generate_profile("account_1", 1, geo={**g, "geo_source": "t", "exit_ip": "1.1.1.1", "checked_at": 1})
    assert pg.validate_profile(p) == []
    p["geo_note"] = None
    assert pg.validate_profile(p)                         # пометка обязательна, молчаливого дефолта нет


# ---------------------------------------------------------------- #
#  Хранилище: постоянство и неизменность
# ---------------------------------------------------------------- #

def test_profile_is_persistent_and_not_overwritten():
    state = _tmp()
    p = _profile()
    path = pg.create_profile(p, state)
    assert pg.load_profile("account_1", state) == p == pg.load_profile("account_1", state)   # «рестарт» не меняет
    assert (path.stat().st_mode & 0o222) == 0                                                  # read-only на диске
    with pytest.raises(pg.ProfileExists):
        pg.create_profile(_profile(seed=99), state)
    assert pg.load_profile("account_1", state) == p


def test_invalid_profile_is_not_saved():
    state = _tmp()
    bad = _profile()
    bad["platform"] = "MacIntel"
    with pytest.raises(pg.InvalidProfile):
        pg.create_profile(bad, state)
    assert not pg.profile_path("account_1", state).exists()


def test_reset_archives_old_profile():
    state = _tmp()
    old = _profile(seed=1)
    pg.create_profile(old, state)
    new = _profile(seed=2)
    pg.reset_profile("account_1", new, state)
    assert pg.load_profile("account_1", state) == new
    hist = list((pg.profiles_dir(state) / ".history").glob("account_1.*.json"))
    assert len(hist) == 1 and json.loads(hist[0].read_text()) == old
    with pytest.raises(pg.ProfileError):
        pg.reset_profile("account_2", new, state)          # чужой профиль


def test_update_geo_touches_only_geo_group():
    state = _tmp()
    p = _profile(impersonate="chrome131", cc="DE")
    pg.create_profile(p, state)
    new_geo = geoip.geo_for_country("JP")
    new_geo.update(geo_source="test", exit_ip="198.51.100.9", checked_at=1_700_100_000)
    upd = pg.update_geo("account_1", new_geo, state)
    assert (upd["country"], upd["locale"], upd["timezone_id"]) == ("JP", "ja-JP", "Asia/Tokyo")
    assert upd["accept_language"].startswith("ja-JP,ja;q=0.9")
    assert pg.fingerprint(upd) == pg.fingerprint(p)                       # устройство то же
    for k in p:
        if k not in pg.GEO_FIELDS and k != "accept_language":
            assert upd[k] == p[k], k


# ---------------------------------------------------------------- #
#  Применение: скрапер и браузер видят одно и то же
# ---------------------------------------------------------------- #

def test_scraper_and_browser_use_same_ua_and_language():
    for imp, os_name in (("chrome150", "windows"), ("firefox147", "linux"), ("safari260", "macos"),
                         ("chrome131_android", "android"), ("safari184_ios", "ios")):
        p = _profile(os_name=os_name, impersonate=imp, cc="RS")
        h, kw = pg.http_headers(p), pg.playwright_context_kwargs(p)
        assert h["User-Agent"] == kw["user_agent"] == p["user_agent"]
        assert h["Accept-Language"] == kw["extra_http_headers"]["Accept-Language"] == p["accept_language"]
        assert kw["locale"] == p["locale"] == "sr-RS" and kw["timezone_id"] == "Europe/Belgrade"
        assert ("sec-ch-ua" in h) == (p["engine"] == "chromium")
        assert kw["viewport"]["width"] == p["viewport"]["w"] and kw["screen"]["width"] == p["screen"]["w"]
        assert kw.get("is_mobile", False) == (os_name in ("android", "ios"))


def test_curl_session_sends_profile_headers():
    p = _profile(impersonate="chrome131", cc="RS")
    s = CurlSessionHandle("chrome131", None, locale=p["locale"])
    assert s.request_headers() == {"Accept-Language": p["accept_language"]}
    s.set_profile_headers(p)
    h = s.request_headers()
    assert h["User-Agent"] == p["user_agent"] and h["sec-ch-ua"] == p["sec_ch_ua"]
    assert h["Accept-Language"] == p["accept_language"]


def test_init_script_fields():
    ch = pg.init_script(_profile(impersonate="chrome131"))
    assert "hardwareConcurrency" in ch and "deviceMemory" in ch and "37445" in ch and "languages" in ch
    ff = pg.init_script(_profile(impersonate="firefox147"))
    assert "deviceMemory" not in ff                                         # у Firefox этого свойства нет


# ---------------------------------------------------------------- #
#  GeoIP (заглушка сети)
# ---------------------------------------------------------------- #

def _fetch_factory(table):
    calls = []

    def fetch(url, proxy, timeout):
        calls.append((url, proxy))
        for key, val in table.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise OSError("no route")
    fetch.calls = calls
    return fetch


def test_geoip_goes_through_slot_proxy_and_matches_country():
    fetch = _fetch_factory({"ipinfo.io": json.dumps({"ip": "185.1.2.3", "country": "RS", "timezone": "Europe/Belgrade"})})
    g = geoip.resolve_geo("http://127.0.0.1:7892", fetch=fetch, now=1000)
    assert fetch.calls == [("https://ipinfo.io/json", "http://127.0.0.1:7892")]    # через прокси слота
    assert (g["country"], g["locale"], g["timezone_id"]) == ("RS", "sr-RS", "Europe/Belgrade")
    assert g["languages"] == ["sr-RS", "sr", "en-US", "en"] and g["exit_ip"] == "185.1.2.3"
    assert g["geo_source"] == "ipinfo" and g["checked_at"] == 1000 and g["geo_known"] is True


def test_geoip_falls_back_to_second_provider_and_uses_service_timezone():
    fetch = _fetch_factory({
        "ipinfo.io": OSError("down"),
        "ip-api.com": json.dumps({"status": "success", "countryCode": "US", "timezone": "America/Los_Angeles", "query": "9.9.9.9"}),
    })
    g = geoip.resolve_geo(None, fetch=fetch)
    assert g["geo_source"] == "ip-api" and g["locale"] == "en-US" and g["timezone_id"] == "America/Los_Angeles"


def test_geoip_unknown_country_and_total_failure():
    fetch = _fetch_factory({"ipinfo.io": json.dumps({"ip": "1.2.3.4", "country": "ZZ"})})
    g = geoip.resolve_geo(None, fetch=fetch)
    assert g["locale"] == "en-US" and g["geo_known"] is False and g["geo_note"]
    with pytest.raises(RuntimeError):
        geoip.resolve_geo(None, fetch=_fetch_factory({}))


def test_country_table_is_consistent():
    for cc, (loc, tz) in geoip.COUNTRY_GEO.items():
        assert loc.split("-")[-1] == cc, (cc, loc)
        assert geoip.valid_timezone(tz), (cc, tz)
    assert geoip.country_from_node_name("uk3.example") == "GB" and geoip.country_from_node_name("rs1") == "RS"
    for cc in ("US", "RS", "BG", "CZ", "JP", "SG", "HU", "BE", "AL", "UA", "DE", "CH", "DK", "LV", "CA", "IN", "GB", "FR", "TR", "NL", "FI", "SK"):
        assert cc in geoip.COUNTRY_GEO                                   # страны нод текущего парка


def test_needs_recheck_on_node_change():
    prof = {"exit_ip": "1.1.1.1", "checked_at": 1000}
    assert geoip.needs_recheck(prof, "1.1.1.1", now=1100) is None
    assert geoip.needs_recheck(prof, "2.2.2.2", now=1100) == "exit_ip_changed"
    assert geoip.needs_recheck(prof, "1.1.1.1", now=1000 + 10 ** 6) == "stale"
    assert geoip.needs_recheck({}, None) == "no_geo"


def test_refresh_geo_updates_only_when_node_changed():
    state = _tmp()
    pg.create_profile(_profile(impersonate="chrome131", cc="DE"), state)       # DE, ip 203.0.113.7
    same = _fetch_factory({"ipinfo.io": json.dumps({"ip": "203.0.113.7", "country": "DE", "timezone": "Europe/Berlin"})})
    assert pg.refresh_geo("account_1", "http://p", fetch=same, state_dir=state) is None
    moved = _fetch_factory({"ipinfo.io": json.dumps({"ip": "5.5.5.5", "country": "JP", "timezone": "Asia/Tokyo"})})
    upd = pg.refresh_geo("account_1", "http://p", fetch=moved, state_dir=state)
    assert (upd["country"], upd["locale"], upd["exit_ip"]) == ("JP", "ja-JP", "5.5.5.5")
    assert pg.load_profile("account_1", state)["locale"] == "ja-JP"
    failing = _fetch_factory({})
    with pytest.raises(RuntimeError):
        pg.refresh_geo("account_1", "http://p", fetch=failing, state_dir=state)
    assert pg.load_profile("account_1", state)["locale"] == "ja-JP"            # профиль не тронут


# ---------------------------------------------------------------- #
#  Миграция account_1..10 и CLI
# ---------------------------------------------------------------- #

def test_migration_keeps_fingerprint_of_existing_accounts():
    from scraper.config import load_accounts
    accs = {a["name"]: a for a in load_accounts()}
    for i in range(1, 11):
        a = accs[f"account_{i}"]
        geo = profile_cli.offline_geo(a, {})
        p = pg.profile_from_account(a, geo, created_at=1)
        assert (p["impersonate"], p["user_agent"]) == (a["impersonate"], a["user_agent"])   # отпечаток прежний
        assert p["locale"] == a["locale"]
        assert pg.validate_profile(p) == [], (a["name"], pg.validate_profile(p))
        assert pg.profile_from_account(a, geo, created_at=1) == p                           # стабильно
    # весь парк из accounts.yaml тоже мигрируется в согласованные профили
    for a in accs.values():
        p = pg.profile_from_account(a, profile_cli.offline_geo(a, {}))
        assert pg.validate_profile(p) == [], (a["name"], pg.validate_profile(p))


def test_cli_plan_apply_skip_and_reset(capsys):
    state = _tmp()
    base = ["--state-dir", str(state), "--accounts", "11-14"]
    assert profile_cli.main(["--create", "--offline", *base]) == 0              # план — ничего не пишет
    assert not list(pg.profiles_dir(state).glob("*.json")) if pg.profiles_dir(state).exists() else True
    assert profile_cli.main(["--create", "--offline", "--apply", "--seed", "42", *base]) == 0
    names = sorted(f.stem for f in pg.profiles_dir(state).glob("account_*.json"))
    assert names == ["account_11", "account_12", "account_13", "account_14"]
    before = {n: pg.load_profile(n, state) for n in names}
    assert profile_cli.main(["--create", "--offline", "--apply", "--seed", "999", *base]) == 0   # повтор: не трогает
    assert {n: pg.load_profile(n, state) for n in names} == before
    assert len({pg.fingerprint(p) for p in before.values()}) == 4
    assert profile_cli.main(["--validate", "--state-dir", str(state)]) == 0

    assert profile_cli.main(["--reset", "account_12", "--state-dir", str(state)]) == 0          # план reset
    assert pg.load_profile("account_12", state) == before["account_12"]
    assert profile_cli.main(["--reset", "account_12", "--apply", "--yes", "--state-dir", str(state)]) == 0
    after = pg.load_profile("account_12", state)
    assert after != before["account_12"] and after["locale"] == before["account_12"]["locale"]   # гео сохранено
    assert list((pg.profiles_dir(state) / ".history").glob("account_12.*.json"))
    assert "сессия Reddit может слететь" in capsys.readouterr().out


def test_cli_migrate_defaults_to_first_ten():
    state = _tmp()
    assert profile_cli.main(["--migrate", "--apply", "--state-dir", str(state)]) == 0
    assert len(list(pg.profiles_dir(state).glob("account_*.json"))) == 10
    from scraper.config import load_accounts
    a1 = next(a for a in load_accounts() if a["name"] == "account_1")
    p1 = pg.load_profile("account_1", state)
    assert p1["impersonate"] == a1["impersonate"] and p1["user_agent"] == a1["user_agent"]
