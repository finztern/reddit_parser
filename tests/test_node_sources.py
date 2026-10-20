"""Переключатель источников узлов VLESS / прокси (scripts/node_sources.py) и пауза аккаунтов планировщиком."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import node_sources as ns  # noqa: E402

VLESS = "vless://00000000-0000-4000-8000-000000000001@v.example.net:443?security=tls#us1"
PROXY = "http://u:p@a.example.net:3128"


def project(tmp: Path, sources=None) -> Path:
    (tmp / "state").mkdir()
    (tmp / "vless_accounts.env").write_text(
        f"# c\naccount_1 = {VLESS}\naccount_2 = {PROXY}\naccount_10 = {PROXY}#x\n", encoding="utf-8")
    cfg = "rotation:\n  slots: []\n" + (f"  node_sources: {sources}\n" if sources is not None else "")
    (tmp / "config.yaml").write_text(cfg, encoding="utf-8")
    return tmp


def test_parse_sources_and_defaults():
    assert ns.parse_sources(["proxy"]) == ["proxy"]
    assert ns.parse_sources("vless") == ["vless"]
    assert ns.parse_sources("all") == ns.parse_sources(None) == ns.parse_sources([]) == ["vless", "proxy"]
    assert ns.parse_sources(["vless", "proxy"]) == ["vless", "proxy"]
    assert ns.parse_sources(["junk"]) == ["vless", "proxy"]


def test_config_then_panel_override_then_clear():
    tmp = project(Path(tempfile.mkdtemp()), "[proxy]")
    assert ns.active_sources(tmp) == ["proxy"]                       # из config.yaml
    ns.set_sources(tmp, ["vless"])
    assert ns.active_sources(tmp) == ["vless"]                       # панель сильнее конфига
    assert ns.clear_override(tmp) == ["proxy"]
    assert ns.active_sources(project(Path(tempfile.mkdtemp()))) == ["vless", "proxy"]   # без ключа — оба


def test_switch_pauses_accounts_of_disabled_source():
    tmp = project(Path(tempfile.mkdtemp()))
    src, off = ns.set_sources(tmp, ["proxy"])
    assert src == ["proxy"] and off == ["account_1"]                 # VLESS-аккаунт на паузе
    data = json.loads((tmp / "state" / ns.DISABLED_FILE).read_text(encoding="utf-8"))
    assert data["disabled"] == ["account_1"]
    _, off = ns.set_sources(tmp, ["vless"])
    assert off == ["account_2", "account_10"]                        # числовой порядок
    _, off = ns.set_sources(tmp, ["vless", "proxy"])
    assert off == []


def test_next_mode_cycle():
    seq = [ns.next_mode(["vless", "proxy"])]
    seq.append(ns.next_mode(seq[-1]))
    seq.append(ns.next_mode(seq[-1]))
    assert seq == [["vless"], ["proxy"], ["vless", "proxy"]]


def test_scheduler_pauses_source_disabled_accounts():
    from scraper.scheduler import read_runtime_disabled
    tmp = project(Path(tempfile.mkdtemp()))
    ns.set_sources(tmp, ["proxy"])
    assert read_runtime_disabled(tmp / "state" / ns.DISABLED_FILE) == {"account_1"}
