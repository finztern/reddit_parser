"""Модуль 01 — парк логин-аккаунтов: convert_to_login.py, add_login_slots.py, режим
node-replace-only демона ротации, статус «нужен вход / проверить вход», отчёт filter_nodes.

Всё на синтетической копии проекта; реальные файлы не трогаются, сети нет (заглушка curl_cffi).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import login_fleet as lf  # noqa: E402
import test_add_guest_slots as tag  # noqa: E402  (фикстура синтетического проекта и заглушка curl_cffi)

EXTRA_SCRIPTS = ("login_fleet.py", "convert_to_login.py", "add_login_slots.py")


def _project(trim: bool = True) -> Path:
    """Копия проекта: слоты 1..25 (или все 55), ноды 1..24, пул из 46 нод."""
    tmp = Path(tempfile.mkdtemp(prefix="loginfleet-"))
    tag.build_fixture(tmp, 9, trim=trim)
    for f in EXTRA_SCRIPTS:
        shutil.copy2(ROOT / "scripts" / f, tmp / "scripts" / f)
    shutil.copy2(ROOT / "scripts" / "refresh_cookies.yaml", tmp / "scripts" / "refresh_cookies.yaml")
    return tmp


def _run(tmp: Path, script: str, *args):
    env = {**os.environ, "PYTHONPATH": str(tmp / "stubs"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(tmp / "scripts" / script), *args],
                          cwd=tmp, env=env, capture_output=True, text=True, timeout=120)


def _load(tmp: Path):
    accounts = yaml.safe_load((tmp / "accounts.yaml").read_text(encoding="utf-8"))["accounts"]
    guest = yaml.safe_load((tmp / "scripts" / "refresh_guest_cookies.yaml").read_text(encoding="utf-8"))
    login = yaml.safe_load((tmp / "scripts" / "refresh_cookies.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((tmp / "config.yaml").read_text(encoding="utf-8"))
    return accounts, guest, login, config


# ---------------------------------------------------------------- #

def test_parse_slot_spec():
    assert lf.parse_slot_spec("11-13") == ["account_11", "account_12", "account_13"]
    assert lf.parse_slot_spec("account_5, 7-8,5") == ["account_5", "account_7", "account_8"]
    for bad in ("x", "5-3", "1-"):
        try:
            lf.parse_slot_spec(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} должен быть отвергнут")


def test_text_patchers_keep_comments_and_are_idempotent():
    acc = ("accounts:\n  # комментарий\n  - name: account_1\n    enabled: true\n    cookie_file: cookies/account_1.json\n"
           "    proxy_port: 7892\n\n  - name: account_2\n    enabled: true\n    cookie_file: cookies/account_2.json\n"
           "    proxy_port: 7891\n")
    once = lf.to_login_account(acc, "account_1")
    assert once == lf.to_login_account(once, "account_1"), "повторный перевод меняет текст"
    d = {a["name"]: a for a in yaml.safe_load(once)["accounts"]}
    assert d["account_1"]["enabled"] is False and d["account_1"]["login"] is True
    assert d["account_1"]["storage_state"] == "storage_state/account_1.json"
    assert d["account_2"] == {"name": "account_2", "enabled": True, "cookie_file": "cookies/account_2.json",
                              "proxy_port": 7891}
    assert "# комментарий" in once

    cfg = "rotation:\n  enabled: true\n  slots:\n    - account_11\n    - account_12\n  # --- воркер ---\n  x: 1\n"
    out = lf.remove_config_slots(cfg, ["account_11", "account_12"])
    assert yaml.safe_load(out)["rotation"]["slots"] == [] and "# --- воркер ---" in out
    out = lf.remove_config_slots(cfg, ["account_11"])
    assert yaml.safe_load(out)["rotation"]["slots"] == ["account_12"]


def test_validate_fleet_finds_problems():
    accs = [{"name": "account_1", "proxy_port": 1, "login": True}, {"name": "account_2", "proxy_port": 1},
            {"name": "account_3", "proxy_port": 3, "login": True}]
    links = {"account_1": "vless://a@h:1#x", "account_2": "vless://a@h:1#y"}
    problems = " | ".join(lf.validate_fleet(accs, links, {"account_3": {}}, {"login_accounts": [], "account_geo": {}},
                                            ["account_1"]))
    for needle in ("дубли proxy_port", "одна нода у нескольких слотов", "одновременно гостевой и login",
                   "нет в login_accounts", "нет account_geo", "в rotation.slots"):
        assert needle in problems, (needle, problems)


def test_login_status_labels():
    acc = {"login": True, "enabled": False}
    assert lf.login_status(acc, {}) == lf.NEED_LOGIN
    acc["enabled"] = True
    assert lf.login_status(acc, {"check_login": True}) == lf.CHECK_LOGIN
    assert lf.login_status(acc, {"needs_node": True, "check_login": True}) == lf.NEED_NODE
    assert lf.login_status(acc, {}) == lf.LOGIN_OK
    assert lf.login_status({"enabled": True}, {"check_login": True}) == ""   # гость


def test_check_login_flag_roundtrip():
    d = Path(tempfile.mkdtemp())
    try:
        assert lf.slot_flags(d, "account_3") == {"needs_node": False, "check_login": False}
        lf.set_check_login(d, "account_3", "node")
        assert lf.slot_flags(d, "account_3")["check_login"] is True
        (d / "identities.json").write_text(json.dumps({"slots": {"account_3": {"needs_node": True}}}))
        assert lf.slot_flags(d, "account_3") == {"needs_node": True, "check_login": True}
        assert lf.clear_check_login(d, "account_3") is True and lf.clear_check_login(d, "account_3") is False
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- #
#  convert_to_login.py
# ---------------------------------------------------------------- #

def test_convert_plan_writes_nothing_and_apply_is_consistent_and_idempotent():
    tmp = _project(trim=False)     # все 55 слотов, как в репозитории
    try:
        files = ("accounts.yaml", "config.yaml", "scripts/refresh_guest_cookies.yaml", "scripts/refresh_cookies.yaml")
        before = {f: (tmp / f).read_text(encoding="utf-8") for f in files}

        r = _run(tmp, "convert_to_login.py", "--all")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[план] ничего не записано" in r.stdout and "Будет переведено слотов: 45" in r.stdout
        for f, text in before.items():
            assert (tmp / f).read_text(encoding="utf-8") == text, f"{f} изменён в режиме плана"

        r = _run(tmp, "convert_to_login.py", "--all", "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        for f in files:
            assert list(tmp.glob(f + ".bak-login-*")) or list((tmp / Path(f).parent).glob(Path(f).name + ".bak-login-*")), f

        accounts, guest, login, config = _load(tmp)
        by = {a["name"]: a for a in accounts}
        assert len(accounts) == 55
        converted = [by[f"account_{n}"] for n in range(11, 56)]
        assert all(a["login"] is True and a["enabled"] is False for a in converted)
        assert all(a["storage_state"] == f"storage_state/{a['name']}.json" for a in converted)
        assert all(a["cookie_file"] == f"cookies/{a['name']}.json" for a in converted)
        orig = {a["name"]: a for a in yaml.safe_load(before["accounts.yaml"])["accounts"]}
        for n in range(1, 11):     # account_1..10 не тронуты
            assert by[f"account_{n}"] == orig[f"account_{n}"]
        assert not guest["guest_accounts"] and not guest.get("account_geo")
        assert config["rotation"]["slots"] == []
        assert set(login["login_accounts"]) == {f"account_{n}" for n in range(1, 56)}
        assert set(login["account_geo"]) == set(login["login_accounts"])
        # гео слотов берётся из прежнего account_geo (account_25 -> sk), а не из значения по умолчанию
        assert login["account_geo"]["account_25"]["locale"] == "sk-SK"
        assert login["account_geo"]["account_12"]["locale"] == "cs-CZ"
        problems = lf.validate_fleet(accounts, lf.load_links(lf.Paths(tmp)), guest["guest_accounts"], login,
                                     config["rotation"]["slots"])
        assert not problems, problems
        assert config["fleet"]["reserve_nodes"] == 6

        # повторный запуск: переводить нечего, файлы не меняются
        snap = {f: (tmp / f).read_text(encoding="utf-8") for f in files}
        r = _run(tmp, "convert_to_login.py", "--all", "--apply")
        assert r.returncode == 0 and "Переводить нечего" in r.stdout, r.stdout + r.stderr
        for f, text in snap.items():
            assert (tmp / f).read_text(encoding="utf-8") == text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_convert_selected_range_and_repo_config_tests_stay_green():
    """--accounts 11-15: остальные гостевые слоты остаются гостевыми; согласованность конфигов
    (tests/test_repo_config.py) на такой копии зелёная."""
    tmp = _project(trim=False)
    try:
        r = _run(tmp, "convert_to_login.py", "--accounts", "11-15", "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        accounts, guest, login, config = _load(tmp)
        assert sorted(guest["guest_accounts"], key=lf.acc_num)[0] == "account_16"
        assert "account_15" not in config["rotation"]["slots"] and "account_16" in config["rotation"]["slots"]
        assert [a["name"] for a in accounts if a.get("login")] == [f"account_{n}" for n in range(11, 16)]

        import test_repo_config as trc
        old = trc.ROOT
        trc.ROOT = tmp
        try:
            trc.test_accounts_are_consistent()
            trc.test_guest_slots_have_harvester_profile_and_rotation()
            trc.test_engine_matches_impersonate()
            trc.test_login_fleet_is_consistent()
        finally:
            trc.ROOT = old

        r = _run(tmp, "convert_to_login.py", "--accounts", "11-12")
        assert "Пропускаю" in r.stdout and "Переводить нечего" in r.stdout
        r = _run(tmp, "convert_to_login.py", "--accounts", "200")
        assert r.returncode == 2 and "нет в accounts.yaml" in r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_convert_rollback_from_backups_restores_guest_mode():
    tmp = _project(trim=False)
    try:
        files = ("accounts.yaml", "config.yaml", "scripts/refresh_guest_cookies.yaml", "scripts/refresh_cookies.yaml")
        before = {f: (tmp / f).read_text(encoding="utf-8") for f in files}
        assert _run(tmp, "convert_to_login.py", "--all", "--apply").returncode == 0
        for f in files:
            bak = sorted((tmp / f).parent.glob(Path(f).name + ".bak-login-*"))[-1]
            shutil.copy2(bak, tmp / f)
            assert (tmp / f).read_text(encoding="utf-8") == before[f]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- #
#  add_login_slots.py
# ---------------------------------------------------------------- #

def _write_ok_file(tmp: Path, n_good: int):
    pool = [l for l in (tmp / "all_nodes.txt").read_text(encoding="utf-8").split()
            if not any(x in l for x in ("dead", "ghost"))]
    (tmp / "all_nodes.reddit_ok.txt").write_text("\n".join(pool[:n_good]) + "\n", encoding="utf-8")
    return pool[:n_good]


def test_add_login_slots_count_equals_good_minus_spare():
    tmp = _project(trim=True)      # слоты 1..25; ноды у 1..24; account_25 без ноды
    try:
        assert _run(tmp, "convert_to_login.py", "--all", "--apply").returncode == 0
        good = _write_ok_file(tmp, 36)
        before_env = (tmp / "vless_accounts.env").read_text(encoding="utf-8")

        r = _run(tmp, "add_login_slots.py", "--seed", "3")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[план] ничего не записано" in r.stdout
        assert (tmp / "vless_accounts.env").read_text(encoding="utf-8") == before_env
        assert "годные 36 - запас 6 = 30 слотов" in r.stdout, r.stdout

        r = _run(tmp, "add_login_slots.py", "--seed", "3", "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        accounts, guest, login, config = _load(tmp)
        by = {a["name"]: a for a in accounts}
        links = lf.load_links(lf.Paths(tmp))

        # число слотов = годные - 6; у каждого слота своя нода и свой порт
        assert len(accounts) == 30 and len(links) == 30 == 36 - 6
        keys = [v.split("#", 1)[0] for v in links.values()]
        assert len(set(keys)) == 30, "одна нода у двух слотов"
        ports = [a["proxy_port"] for a in accounts]
        assert len(set(ports)) == len(ports), "дубли портов"
        new = [by[f"account_{n}"] for n in range(26, 31)]
        assert sorted(a["proxy_port"] for a in new) == list(range(7946, 7951))
        assert all(a["login"] is True and a["enabled"] is False for a in new)
        assert all(a["storage_state"] == f"storage_state/{a['name']}.json" for a in new)
        assert by["account_25"]["enabled"] is False and "account_25" in links     # слот без ноды получил ноду первым
        # выданы только ГОДНЫЕ ноды (из reddit_ok), страны есть в гео
        good_keys = {g.split("#", 1)[0] for g in good}
        given = [links[f"account_{n}"].split("#", 1)[0] for n in range(25, 31)]
        assert set(given) <= good_keys
        assert set(login["login_accounts"]) == {a["name"] for a in accounts}   # account_1..10 + все слоты
        assert all(n in login["account_geo"] for n in login["login_accounts"])
        # гостевые файлы и rotation.slots не тронуты
        assert not guest["guest_accounts"] and config["rotation"]["slots"] == []
        problems = lf.validate_fleet(accounts, links, guest["guest_accounts"], login, config["rotation"]["slots"])
        assert not problems, problems
        assert list(tmp.glob("vless_accounts.env.bak-addlogin-*"))

        # повторный запуск ничего не добавляет: слотов уже good - spare
        r = _run(tmp, "add_login_slots.py", "--seed", "3", "--apply")
        assert r.returncode == 0 and "Изменений нет" in r.stdout, r.stdout + r.stderr
        assert len(_load(tmp)[0]) == 30
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_add_login_slots_needs_filter_output_and_keeps_spare():
    tmp = _project(trim=True)
    try:
        assert _run(tmp, "convert_to_login.py", "--all", "--apply").returncode == 0
        r = _run(tmp, "add_login_slots.py")
        assert r.returncode == 2 and "filter_nodes.py --write" in r.stdout, r.stdout + r.stderr

        # свободных нод мало: после запаса остаётся не меньше spare
        _write_ok_file(tmp, 33)       # цель 27 слотов: 24 с нодой -> +3
        r = _run(tmp, "add_login_slots.py", "--spare", "6", "--seed", "1", "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        accounts, *_ = _load(tmp)
        links = lf.load_links(lf.Paths(tmp))
        assert len(links) == 27
        ok_keys = {l.split("#", 1)[0] for l in (tmp / "all_nodes.reddit_ok.txt").read_text(encoding="utf-8").split()}
        free_left = ok_keys - {v.split("#", 1)[0] for v in links.values()}
        assert len(free_left) >= 6, "запас на замену съеден"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_counts_math():
    from add_login_slots import allocate_login_ports, plan_counts

    assert plan_counts(good=36, spare=6, have=24, missing=1, free=36) == \
        {"target": 30, "budget": 6, "take": 6, "n_missing": 1, "n_new": 5}
    c = plan_counts(good=10, spare=6, have=2, missing=0, free=3)      # свободных сверх запаса нет
    assert c["take"] == 0
    assert plan_counts(good=3, spare=6, have=0, missing=0, free=3)["target"] == 0
    assert allocate_login_ports({7945, 7900}, 3, check=lambda p: p != 7947) == [7946, 7948, 7949]


# ---------------------------------------------------------------- #
#  демон: node-replace-only
# ---------------------------------------------------------------- #

class FakeMihomo:
    def __init__(self):
        self.selected = {}
        self.closed = []

    def select(self, group, node):
        self.selected[group] = node

    def close_inbound(self, port):
        self.closed.append(port)


def _daemon(tmp: Path, ok_keys, bad=()):
    """Daemon без сети: нода account_1 сгорела, в пуле n2 (годная), n3 (негодная), n4 (чужая идентичность)."""
    sys.path.insert(0, str(tmp / "stubs"))
    import rotation_daemon as rd
    from scraper import identity as idn

    idn.STATE_DIR, idn.SLOTS_DIR = tmp / "state", tmp / "state" / "slots"
    rd.IDENT_FILE = tmp / "state" / "identities.json"
    d = rd.Daemon(SimpleNamespace(no_headless=True))
    d.rc = {**rd.DEFAULTS}
    d.fleet = dict(lf.FLEET_DEFAULTS)
    d.login_set = {"account_1"}
    d.accounts = {"account_1": {"name": "account_1", "enabled": True, "login": True, "proxy_port": 7892}}
    k = {i: f"vless://uuid{i}@h{i}:443?x=1" for i in range(1, 6)}
    d.env = SimpleNamespace(
        assigned={"account_1": k[1] + "#rs1"},
        key2name={k[i]: f"n{i}" for i in range(1, 6)},
        key2uri={k[i]: k[i] + f"#n{i}" for i in range(1, 6)},
    )
    d.ok_keys = {k[i] for i in ok_keys}
    fp = {"engine": "firefox", "channel": None, "device": None, "user_agent": "UA/1", "impersonate": "firefox147",
          "label": "seed"}
    prev = d._make_identity(k[1], k[1] + "#rs1", fp, {"locale": "sr-RS", "timezone_id": "Europe/Belgrade"})
    prev["active_since"] = time.time() - 3600
    other = d._make_identity(k[4], k[4] + "#n4", {**fp, "impersonate": "chrome131"},
                             {"locale": "en-US", "timezone_id": "America/New_York"})
    d.st["identities"] = {prev["id"]: prev, other["id"]: other}
    d.st["slots"] = {"account_1": {"active": prev["id"], "rotations": [], "rev": 3}}
    for i in bad:
        d.st["bad_nodes"][k[i]] = {"ts": time.time(), "ttl_hours": 24, "reason": "t", "name": f"n{i}"}
    d.mihomo = FakeMihomo()
    return d, rd, k, prev


def test_daemon_replaces_node_keeps_profile_and_session():
    tmp = _project(trim=True)
    try:
        import rotation as rot

        d, rd, k, prev = _daemon(tmp, ok_keys=[2, 4, 5])     # n3 не прошла filter_nodes; n4 — чужая идентичность
        cookies = tmp / "cookies" / "account_1.json"
        cookies.parent.mkdir(exist_ok=True)
        cookies.write_text('[{"name":"session","value":"keep-me"}]', encoding="utf-8")
        before = cookies.read_text(encoding="utf-8")
        calls = []
        rot.precheck = lambda port, timeout=12: (calls.append(port) or (True, "ip=1.2.3.4 reddit=200"))

        for _ in range(8):      # результат случайного выбора не важен: годятся только n2 и n5
            d2, rd, k, prev2 = _daemon(tmp, ok_keys=[2, 4, 5])
            assert d2.replace_node("account_1") is True
            slot = d2.st["slots"]["account_1"]
            ident = d2.st["identities"][slot["active"]]
            assert ident["node_key"] in (k[2], k[5]), "взята негодная/чужая/уже назначенная нода"
            assert ident["fp"]["impersonate"] == "firefox147" and ident["fp"]["user_agent"] == "UA/1", \
                "отпечаток слота изменился"
            assert slot["needs_node"] is False and slot["rev"] == 4
            assert d2.st["identities"][prev2["id"]]["banned_at"] and \
                d2.st["identities"][prev2["id"]]["ban_reason"] == "node_burned"
            prof = json.loads((tmp / "state" / "slots" / "account_1.json").read_text(encoding="utf-8"))
            assert prof["impersonate"] == "firefox147" and prof["user_agent"] == "UA/1"
            assert prof["node"] in ("n2", "n5") and prof["rev"] == 4
            assert d2.mihomo.selected["pg-account_1"] == prof["node"] and 7892 in d2.mihomo.closed
            assert lf.slot_flags(tmp / "state", "account_1")["check_login"] is True   # «проверить вход»
            assert cookies.read_text(encoding="utf-8") == before, "cookies/сессия тронуты"
            lf.clear_check_login(tmp / "state", "account_1")
        assert calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_daemon_needs_node_when_reserve_empty_then_recovers():
    tmp = _project(trim=True)
    try:
        import rotation as rot

        d, rd, k, prev = _daemon(tmp, ok_keys=[], bad=())        # годных нод в запасе нет
        d.ok_keys = set()
        rot.precheck = lambda port, timeout=12: (True, "ip=1 reddit=200")
        assert d.replace_node("account_1") is False
        slot = d.st["slots"]["account_1"]
        assert slot["needs_node"] is True and slot["active"] == prev["id"], "слот должен остаться на прежней идентичности"
        assert d.mihomo.selected.get("pg-account_1") == "n1", "mihomo не возвращён на прежнюю ноду"
        assert lf.slot_flags(tmp / "state", "account_1")["needs_node"] is True   # видно в «панели»
        assert lf.login_status({**d.accounts["account_1"]}, lf.slot_flags(tmp / "state", "account_1")) == lf.NEED_NODE

        # запас пополнили (filter_nodes) -> демон сам повторяет попытку
        d.ok_keys = {k[2]}
        slot["needs_node_retry"] = 0
        d.retry_needs_node()
        assert d.st["slots"]["account_1"]["needs_node"] is False
        assert d.st["identities"][d.st["slots"]["account_1"]["active"]]["node_key"] == k[2]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_daemon_dead_replacement_goes_to_bad_nodes_and_next_is_tried():
    tmp = _project(trim=True)
    try:
        import rotation as rot

        d, rd, k, prev = _daemon(tmp, ok_keys=[2, 5])
        alive = {"n2": False, "n5": True}
        rot.precheck = lambda port, timeout=12: ((alive[d.mihomo.selected["pg-account_1"]], "info"))
        assert d.replace_node("account_1") is True
        assert d.st["identities"][d.st["slots"]["account_1"]["active"]]["node_key"] == k[5]
        assert k[2] in d.st["bad_nodes"] or d.st["slots"]["account_1"]["active"]   # n2 мог не выпасть первой
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_daemon_handle_login_limits_and_guest_off():
    tmp = _project(trim=True)
    try:
        d, rd, k, prev = _daemon(tmp, ok_keys=[2])
        # сутки: лимит замен
        d.st["slots"]["account_1"]["rotations"] = [time.time()] * d.rc["max_rotations_per_day"]
        try:
            d.handle("account_1", {"status": "pending", "ts": time.time()})
            raise AssertionError("лимит замен не сработал")
        except rd.Reject as e:
            assert "лимит" in e.reason
        # гостевой слот при guest_enabled: false отклоняется
        d.accounts["account_20"] = {"name": "account_20", "enabled": True, "proxy_port": 7910}
        try:
            d.handle("account_20", {"status": "pending", "ts": time.time()})
            raise AssertionError("гостевая заявка должна быть отклонена")
        except rd.Reject as e:
            assert "guest_enabled" in e.reason
        assert d.guest_names() == []
        d.fleet["guest_enabled"] = True
        d.gcfg = {"guest_accounts": {"account_20": {}}}
        assert d.guest_names() == ["account_20"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_daemon_seeds_login_slots_from_accounts_yaml():
    tmp = _project(trim=True)
    try:
        d, rd, k, prev = _daemon(tmp, ok_keys=[2])
        d.st["slots"], d.st["identities"] = {}, {}
        d.gcfg = {"guest_accounts": {}, "account_geo": {}}
        d.login_cfg = {"account_geo": {"account_1": {"locale": "sr-RS", "timezone_id": "Europe/Belgrade"}}}
        d.accounts["account_1"].update(impersonate="firefox147", user_agent="UA/1")
        d.ensure_seeds()
        slot = d.st["slots"]["account_1"]
        ident = d.st["identities"][slot["active"]]
        assert ident["fp"]["impersonate"] == "firefox147" and ident["geo"]["locale"] == "sr-RS"
        assert ident["fp"]["engine"] == "firefox"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- #
#  filter_nodes: отчёт «в панель»
# ---------------------------------------------------------------- #

def test_filter_nodes_report():
    sys.path.insert(0, str(ROOT / "tests"))
    tmp = _project(trim=True)
    try:
        sys.path.insert(0, str(tmp / "stubs"))
        import filter_nodes as fn

        rep = fn.build_report(40, 12, 27, 1, {"us": [3, 5]}, lf.FLEET_DEFAULTS)
        assert rep["good"] == 12 and rep["slots_target"] == 6 and rep["warn"] is False
        assert fn.build_report(40, 9, 30, 1, {}, lf.FLEET_DEFAULTS)["warn"] is True      # < min_good_nodes_warn
        assert fn.build_report(40, 4, 35, 1, {}, lf.FLEET_DEFAULTS)["slots_target"] == 0
        path = tmp / "state" / "node_report.json"
        fn.write_report(path, rep)
        assert json.loads(path.read_text(encoding="utf-8"))["good"] == 12
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
