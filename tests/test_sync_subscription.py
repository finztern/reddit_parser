"""scripts/sync_subscription.py: метки узлов, закрепление за аккаунтами, замена пропавших, пул, применение и откат."""
import base64
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import sync_subscription as ss  # noqa: E402

UUID = "00000000-0000-0000-0000-000000000000"
SECRET_URL = "https://sub.example.test/get?u=1&c=SECRETTOKEN&s=vless"


def link(label, host="gw.example.test", uuid=UUID, frag=None):
    frag = label + ".example" if frag is None else frag
    return (f"vless://{uuid}@{host}:443?security=tls&type=httpupgrade&host={host}&sni={host}&fp=chrome"
            f"&path=%2F{label}%2F" + (f"#{frag}" if frag else ""))


def sub_of(*labels, **kw):
    return {l: link(l, **kw) for l in labels}


def many(*extra):
    """>= MIN_NODES узлов, чтобы пройти предохранитель."""
    base = [f"xx{i}" for i in range(1, 11)]
    return sub_of(*base, *extra)


def make_root(env_lines, pool=None, ok=None):
    d = Path(tempfile.mkdtemp())
    (d / "vless_accounts.env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    if pool is not None:
        (d / "all_nodes.txt").write_text("\n".join(pool) + "\n", encoding="utf-8")
    if ok is not None:
        (d / "all_nodes.reddit_ok.txt").write_text("\n".join(ok) + "\n", encoding="utf-8")
    return d


class Runner:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, cmd, cwd):
        self.calls.append(cmd)
        return self.rc if "generate_mihomo_config.py" in " ".join(cmd) else 0


def test_label_and_sig():
    u = link("us23-5")
    assert ss.label_of(u) == "us23-5"
    assert ss.label_of("vless://u@h:443?type=tcp#fi1.example") == "fi1"
    assert ss.sig_of(u) == "gw.example.test:443|gw.example.test"


def test_parse_plain_base64_bad_and_dups():
    text = "\n".join([link("de2"), link("fi1"), link("de2"), "vless://broken", "junk"])
    sub, st = ss.parse_subscription(text)
    assert list(sub) == ["de2", "fi1"]
    assert st["dups"] == 1 and st["bad"] == 1
    b64, _ = ss.parse_subscription(base64.b64encode("\n".join([link("de2"), link("fi1")]).encode()).decode())
    assert list(b64) == ["de2", "fi1"]


def test_plan_no_changes():
    sub = sub_of("de2", "fi1")
    new, act = ss.plan_sync({"account_1": sub["de2"], "account_2": sub["fi1"]}, sub)
    assert new == {} and act == []


def test_plan_updates_changed_link_keeps_label_and_name():
    sub = sub_of("de2", "fi1")
    old = link("de2", uuid="11111111-1111-1111-1111-111111111111", frag="de2~2")
    new, act = ss.plan_sync({"account_1": old}, sub)
    assert act == [{"account": "account_1", "kind": "update", "from": "de2", "to": "de2"}]
    assert new["account_1"] == ss.rot.node_key(sub["de2"]) + "#de2~2"


def test_plan_vanished_gets_same_country_free_node_by_rank():
    sub = sub_of("de3", "de4", "fi1", "fr1")
    env = {"account_1": link("de2"), "account_2": sub["de3"]}
    rank = {ss.rot.node_key(sub["de4"]): 0}
    new, act = ss.plan_sync(env, sub, known_labels=["de2"], ok_rank=rank)
    assert act == [{"account": "account_1", "kind": "replace", "from": "de2", "to": "de4"}]
    assert ss.label_of(new["account_1"]) == "de4"          # de3 занят account_2, fi1/fr1 — чужая страна


def test_plan_vanished_without_same_country_and_cross_country_flag():
    sub = sub_of("fi1", "fr1")
    env = {"account_1": link("de2")}
    new, act = ss.plan_sync(env, sub, known_labels=["de2"])
    assert new == {} and act[0]["kind"] == "no_replacement" and act[0]["to"] is None
    new, act = ss.plan_sync(env, sub, known_labels=["de2"], cross_country=True)
    assert act[0]["kind"] == "replace" and act[0]["to"] == "fi1"


def test_plan_ignores_foreign_links_and_two_accounts_get_different_nodes():
    sub = sub_of("de3", "de4")
    foreign = "vless://u@other.example.test:443?security=tls&type=tcp#us1"
    env = {"account_1": link("de1"), "account_2": link("de2"), "account_3": foreign,
           "account_4": "http://u:p@1.2.3.4:80#proxy"}
    new, act = ss.plan_sync(env, sub, known_labels=["de1", "de2"])
    assert set(new) == {"account_1", "account_2"}
    assert {ss.label_of(new["account_1"]), ss.label_of(new["account_2"])} == {"de3", "de4"}


def test_merge_pool_replaces_sub_links_keeps_others():
    sub = sub_of("de2", "fi1")
    stale = link("de2", uuid="11111111-1111-1111-1111-111111111111")
    other = "vless://u@other.example.test:443?security=tls&type=tcp#keep"
    out = ss.merge_pool([stale, other], sub)
    assert other in out and stale not in out and sub["de2"] in out and sub["fi1"] in out


def _sync(root, sub, **kw):
    lines = []
    runner = kw.pop("run", None) or Runner()
    rc = ss.sync(root, SECRET_URL, apply=kw.pop("apply", True), fetcher=lambda u: "\n".join(sub.values()),
                 run=runner, out=lines.append, **kw)
    return rc, lines, runner


def test_plan_mode_writes_nothing_and_never_prints_url():
    sub = many("de2")
    root = make_root(["account_1 = " + link("de2", uuid="11111111-1111-1111-1111-111111111111")])
    before = (root / "vless_accounts.env").read_text()
    rc, lines, runner = _sync(root, sub, apply=False)
    assert rc == 0 and runner.calls == []
    assert (root / "vless_accounts.env").read_text() == before
    assert not (root / "state" / ss.STATE_FILE).exists()
    assert "SECRETTOKEN" not in "\n".join(lines) and UUID not in "\n".join(lines)


def test_apply_updates_env_pool_state_backups_and_restarts():
    sub = many("de2")
    old = link("de2", uuid="11111111-1111-1111-1111-111111111111", frag="de2")
    root = make_root(["# note", "account_1 = " + old, "account_2 = http://u:p@1.2.3.4:80#px"], pool=[old])
    rc, lines, runner = _sync(root, sub)
    assert rc == 0
    env = (root / "vless_accounts.env").read_text()
    assert "# note" in env and "http://u:p@1.2.3.4:80#px" in env
    assert ss.rot.node_key(sub["de2"]) in env and "11111111-1111" not in env
    assert ss.rot.node_key(sub["de2"]) in (root / "all_nodes.txt").read_text()
    assert list(root.glob("vless_accounts.env.bak-sub-*"))
    st = json.loads((root / "state" / ss.STATE_FILE).read_text())
    assert "de2" in st["labels"] and st["count"] == len(sub)
    assert "SECRETTOKEN" not in json.dumps(st)
    assert any("generate_mihomo_config.py" in " ".join(c) for c in runner.calls)
    assert runner.calls[-1] == ["docker", "compose", "restart", "mihomo"]


def test_second_run_without_changes_does_not_restart():
    sub = many("de2")
    root = make_root(["account_1 = " + sub["de2"]], pool=list(sub.values()))
    rc, lines, runner = _sync(root, sub)
    assert rc == 0 and runner.calls == [] and "Изменений нет." in lines


def test_generator_failure_rolls_back():
    sub = many("de2")
    old = link("de2", uuid="11111111-1111-1111-1111-111111111111")
    root = make_root(["account_1 = " + old], pool=[old])
    rc, lines, runner = _sync(root, sub, run=Runner(rc=1))
    assert rc == 1
    assert (root / "vless_accounts.env").read_text() == f"account_1 = {old}\n"
    assert (root / "all_nodes.txt").read_text() == old + "\n"
    assert all("restart" not in c for c in runner.calls)


def test_guard_rejects_small_or_collapsed_subscription():
    root = make_root(["account_1 = " + link("de2")])
    rc, lines, runner = _sync(root, sub_of("fi1"))
    assert rc == 2 and runner.calls == []
    # ранее видели 20 меток, пришло 9 (меньше половины) — сбой даже при низком --min-nodes
    (root / "state").mkdir(exist_ok=True)
    (root / "state" / ss.STATE_FILE).write_text(json.dumps({"labels": [f"l{i}" for i in range(20)]}))
    rc, lines, _ = _sync(root, sub_of(*[f"y{i}" for i in range(9)]), min_nodes=5)
    assert rc == 2


def test_fetch_error_message_has_no_url():
    root = make_root(["account_1 = " + link("de2")])
    lines = []

    def boom(url):
        raise RuntimeError("failed " + url)

    rc = ss.sync(root, SECRET_URL, apply=True, fetcher=boom, run=Runner(), out=lines.append)
    assert rc == 2 and "SECRETTOKEN" not in "\n".join(lines)


def test_read_url_priority_and_main_unconfigured():
    root = Path(tempfile.mkdtemp())
    (root / ss.URL_FILE).write_text("# c\nhttps://x.example.test/s\n")
    saved = ss.os.environ.pop(ss.URL_ENV, None)
    try:
        assert ss.read_url(root) == "https://x.example.test/s"
        assert ss.read_url(root, "https://cli.example.test/") == "https://cli.example.test/"
        (root / ss.URL_FILE).unlink()
        assert ss.main(["--root", str(root)]) == 3
    finally:
        if saved is not None:
            ss.os.environ[ss.URL_ENV] = saved
