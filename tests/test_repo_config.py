"""Согласованность конфигов репозитория: accounts.yaml <-> refresh_guest_cookies.yaml
<-> config.yaml (rotation.slots) <-> systemd-шаблоны. Ловит рассинхрон, который
иначе проявляется только на сервере (дубли портов, слот без профиля харвестера,
слот вне ротации, устаревшая ссылка на юнит в документации)."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _load():
    accounts = yaml.safe_load((ROOT / "accounts.yaml").read_text(encoding="utf-8"))["accounts"]
    guest_cfg = yaml.safe_load((ROOT / "scripts" / "refresh_guest_cookies.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    login = yaml.safe_load((ROOT / "scripts" / "refresh_cookies.yaml").read_text(encoding="utf-8"))
    return accounts, guest_cfg, config, login


def _num(name):
    return int(name.rsplit("_", 1)[1])


def test_accounts_are_consistent():
    accounts, _, _, _ = _load()
    names = [a["name"] for a in accounts]
    assert len(names) == len(set(names))
    ports = [a["proxy_port"] for a in accounts]
    assert len(ports) == len(set(ports)), "дубли proxy_port"
    for a in accounts:
        assert a["cookie_file"] == f"cookies/{a['name']}.json", a["name"]
        assert a.get("impersonate") and a.get("user_agent"), a["name"]


def test_guest_slots_have_harvester_profile_and_rotation():
    accounts, guest_cfg, config, login = _load()
    guests = {a["name"]: a for a in accounts if _num(a["name"]) >= 11}
    spec = guest_cfg["guest_accounts"]
    assert set(spec) == set(guests), "guest_accounts и гостевые слоты accounts.yaml разошлись"
    assert set(config["rotation"]["slots"]) == set(guests), "rotation.slots и гостевые слоты разошлись"
    assert all(a["enabled"] for a in guests.values()), "все гостевые слоты должны быть включены"
    # accounts.yaml — источник правды: отпечаток харвестера совпадает с ним
    for name, a in guests.items():
        assert spec[name]["impersonate"] == a["impersonate"], name
        assert spec[name]["user_agent"] == a["user_agent"], name
    # логин-джоб не должен трогать гостей
    assert not (set(login["login_accounts"]) & set(guests))


def test_engine_matches_impersonate():
    _, guest_cfg, _, _ = _load()
    expected = {"firefox": "firefox", "safari": "webkit", "chrome": "chromium"}
    for name, s in guest_cfg["guest_accounts"].items():
        fam = re.match(r"[a-z]+", s["impersonate"]).group(0)
        assert s["engine"] == expected[fam], (name, s["engine"], s["impersonate"])


def test_impersonate_targets_parse():
    import rotate_fingerprints as rf

    accounts, _, _, _ = _load()
    for a in accounts:
        assert rf.parse_target(a["impersonate"]) is not None, (a["name"], a["impersonate"])


def test_systemd_templates():
    units = sorted((ROOT / "scripts" / "systemd").iterdir())
    names = {u.name for u in units}
    assert {"refresh-cookies.service", "refresh-cookies.timer", "refresh-guest-cookies.service",
            "refresh-guest-cookies.timer", "rotation-daemon.service"} <= names
    for u in units:
        text = u.read_text(encoding="utf-8")
        assert "/home/sashko" not in text, f"{u.name}: вшит путь/пользователь"
        if u.suffix == ".service":
            for ph in ("@USER@", "@GROUP@", "@ROOT@"):
                assert ph in text, f"{u.name}: нет плейсхолдера {ph}"
    # документация ссылается только на существующие юниты
    for doc in ("README.md", "ROTATION_SETUP.md", "DEPLOY.md"):
        p = ROOT / doc
        if p.exists():
            for ref in re.findall(r"scripts/systemd/([\w.-]+\.(?:service|timer))", p.read_text(encoding="utf-8")):
                assert ref in names, f"{doc} ссылается на несуществующий scripts/systemd/{ref}"
