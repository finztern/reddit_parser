"""scripts/reset_slots.py: полная очистка слотов с бэкапами и переносом per-account файлов."""
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import reset_slots as rs  # noqa: E402

ACCOUNTS = """accounts:
  # шапка
  - name: account_1
    enabled: true
    proxy_port: 7892
  - name: account_2
    enabled: false
    proxy_port: 7893
"""
LOGIN = """login_accounts:
  - account_1
  - account_2
headless: true
account_geo:
  account_1:  {locale: "de-DE", timezone_id: "Europe/Berlin"}
  account_2:  {locale: "fi-FI", timezone_id: "Europe/Helsinki"}
"""
GUEST = """headless: true
account_geo:
  account_3:  {locale: "en-US", timezone_id: "America/New_York"}
guest_accounts:
  account_3: {engine: chromium}
"""
CONFIG = """rotation:
  slots:
    - account_3
fleet: {}
"""


def make_root():
    d = Path(tempfile.mkdtemp())
    (d / "scripts").mkdir()
    (d / "accounts.yaml").write_text(ACCOUNTS, encoding="utf-8")
    (d / "scripts" / "refresh_cookies.yaml").write_text(LOGIN, encoding="utf-8")
    (d / "scripts" / "refresh_guest_cookies.yaml").write_text(GUEST, encoding="utf-8")
    (d / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (d / "vless_accounts.env").write_text("# c\naccount_1 = vless://u@h:443?type=tcp#a\naccount_2 = http://u:p@1.1.1.1:80#b\n")
    for sub in ("cookies", "storage_state", "state/slots"):
        (d / sub).mkdir(parents=True)
    (d / "cookies" / "account_1.json").write_text("[]")
    (d / "cookies" / "keep.txt").write_text("x")
    (d / "state" / "slots" / "account_1.json").write_text("{}")
    (d / "state" / "identities.json").write_text("{}")
    (d / "state" / "rotation_state.json").write_text("{}")
    return d


def test_plan_changes_nothing():
    d = make_root()
    lines = []
    assert rs.reset(d, apply=False, out=lines.append) == 0
    assert (d / "accounts.yaml").read_text(encoding="utf-8") == ACCOUNTS
    assert (d / "cookies" / "account_1.json").exists() and (d / "state" / "identities.json").exists()
    assert any("план" in l for l in lines)


def test_apply_clears_slots_keeps_headers_pool_state_and_archives_files():
    d = make_root()
    rs.reset(d, apply=True, out=lambda *_: None)
    acc = yaml.safe_load((d / "accounts.yaml").read_text(encoding="utf-8"))
    assert not acc["accounts"] and "# шапка" in (d / "accounts.yaml").read_text(encoding="utf-8")
    assert (d / "vless_accounts.env").read_text() == "# c\n"
    login = yaml.safe_load((d / "scripts" / "refresh_cookies.yaml").read_text(encoding="utf-8"))
    assert not login["login_accounts"] and not login["account_geo"] and login["headless"] is True
    assert yaml.safe_load((d / "scripts" / "refresh_guest_cookies.yaml").read_text(encoding="utf-8"))["guest_accounts"] == {}
    assert yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8"))["rotation"]["slots"] == []
    arch = next((d / "state").glob("reset-*"))
    assert (arch / "cookies" / "account_1.json").exists() and (arch / "state" / "identities.json").exists()
    assert not (d / "cookies" / "account_1.json").exists() and (d / "cookies" / "keep.txt").exists()
    assert (d / "state" / "rotation_state.json").exists()                 # список плохих нод не трогаем
    assert list(d.glob("accounts.yaml.bak-reset-*"))


def test_second_run_is_noop():
    d = make_root()
    rs.reset(d, apply=True, out=lambda *_: None)
    lines = []
    rs.reset(d, apply=True, out=lines.append)
    assert any("Нечего убирать" in l for l in lines)
    assert len(list((d / "state").glob("reset-*"))) == 1
