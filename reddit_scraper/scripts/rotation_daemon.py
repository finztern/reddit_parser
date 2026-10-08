#!/usr/bin/env python3
"""
scripts/rotation_daemon.py — демон ротации идентичностей гостевых слотов.
Работает НА ХОСТЕ (рядом с mihomo и Playwright), не в контейнере.

Идентичность = нода (VLESS) + отпечаток (impersonate/UA/engine/device) + гео.
Cookies в неё не входят — их харвестим заново при каждой смене.

Поток:
  1. Воркер видит N пустых листингов подряд и кладёт заявку
     state/requests/account_N.json (status=pending), сам уходит на паузу.
  2. Демон: текущую идентичность слота помечает забаненной (banned_at),
     выбирает следующую:
        - сначала "целинные" ноды (из all_nodes.txt и нод слотов, которых
          ещё нет в реестре) с новым отпечатком;
        - потом "отлежавшиеся" — забаненные давно (>= ban_cooldown_hours),
          самая старая первой, с ТЕМ ЖЕ отпечатком, что была у неё раньше.
     Ноды заняты эксклюзивно (одна нода — один слот), ноды логин-аккаунтов
     1..10 не трогаются.
  3. Переключает ноду в mihomo (PUT /proxies/pg-account_N), проверяет, что
     нода жива, харвестит cookies Playwright'ом (process_guest_account из
     refresh_guest_cookies.py), верифицирует comments.json.
  4. Только при успехе: пишет state/slots/account_N.json (профиль), затем
     атомарно кладёт cookies в cookies/account_N.json, затем status=done.
     Воркер подхватывает всё сам.

Переживает рестарты контейнеров: всё состояние в state/. Каждые
reconcile_seconds демон сверяет выбор ноды в mihomo с реестром — если
mihomo перезапускали (select сбрасывается на первую ноду), выбор
восстанавливается.

Запуск (под xvfb, см. scripts/systemd/rotation-daemon.service):
    venv/bin/python3 scripts/rotation_daemon.py --no-headless
    venv/bin/python3 scripts/rotation_daemon.py --status
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import math
import os
import random
import sys
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import generate_mihomo_config as gmc  # noqa: E402
import refresh_guest_cookies as rgc  # noqa: E402
import rotate_fingerprints as rf  # noqa: E402
import rotation as rot  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from scraper import identity as idn  # noqa: E402
from scraper.constants import log  # noqa: E402

IDENT_FILE = idn.STATE_DIR / "identities.json"
STAGING_REL = "state/staging"
BROWSER_LOCK = "/tmp/reddit_scraper_browser.lock"  # общий с systemd-джобами

DEFAULTS = {
    "enabled": True,
    "slots": [],
    "nodes_file": "all_nodes.txt",
    "mihomo_api": "http://127.0.0.1:9090",
    "mihomo_secret": "",
    "request_cooldown_seconds": 600,
    "ban_cooldown_hours": 6,
    "max_rotations_per_day": 6,
    "max_candidates_per_request": 4,
    "bad_node_ttl_hours": 24,
    "global_outage_fraction": 0.5,
    "global_outage_pause_seconds": 900,
    "failed_all_pause_seconds": 900,
    "poll_seconds": 5,
    "reconcile_seconds": 30,
    "lock_timeout_seconds": 1500,
}


class Reject(Exception):
    def __init__(self, reason: str, retry_after: float):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


# ---------------------------------------------------------------- #
#  Конфиг / состояние
# ---------------------------------------------------------------- #

def load_rcfg() -> dict:
    try:
        data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("config.yaml не прочитан (%s) — беру дефолты", e)
        data = {}
    return {**DEFAULTS, **(data.get("rotation") or {})}


def load_state() -> dict:
    st = idn.read_json(IDENT_FILE) or {}
    st.setdefault("identities", {})
    st.setdefault("slots", {})
    st.setdefault("bad_nodes", {})
    st.setdefault("ban_lifetimes", [])
    return st


def save_state(st: dict) -> None:
    st["ban_lifetimes"] = st["ban_lifetimes"][-200:]
    idn.write_json_atomic(IDENT_FILE, st)


def ident_id(node_key: str) -> str:
    return hashlib.sha1(node_key.encode()).hexdigest()[:10]


def node_label(uri: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(uri).fragment) or uri[-24:]


class BrowserLock:
    """Тот же lock, что у systemd-джобов refresh_*: один браузер за раз."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.f = None

    def __enter__(self):
        self.f = open(BROWSER_LOCK, "a")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() > deadline:
                    self.f.close()
                    raise Reject("не дождался browser-lock (идёт refresh-джоб)", time.time() + 300)
                time.sleep(2)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
        except Exception:
            pass


# ---------------------------------------------------------------- #
#  Демон
# ---------------------------------------------------------------- #

class Daemon:
    def __init__(self, args):
        self.args = args
        self.st = load_state()
        self.fp_args = SimpleNamespace(allow_old=False, no_edge=True, no_mobile=False)
        self.mihomo: rot.Mihomo | None = None
        self.rc = load_rcfg()
        self.accounts: dict = {}
        self.gcfg: dict = {}
        self.env = None
        self.reserved_keys: set[str] = set()

    # ---- контекст (перечитываем файлы на каждой итерации) ----
    def refresh_ctx(self) -> None:
        self.rc = load_rcfg()
        self.mihomo = rot.Mihomo(self.rc["mihomo_api"], self.rc.get("mihomo_secret", ""))
        data = yaml.safe_load((ROOT / "accounts.yaml").read_text(encoding="utf-8")) or {}
        self.accounts = {a["name"]: a for a in data.get("accounts", [])}
        self.gcfg = rgc.load_guest_config(rgc._SCRIPTS_DIR / "refresh_guest_cookies.yaml")
        if self.args.no_headless:
            self.gcfg["headless"] = False

        assigned = gmc.load_vless_links()
        key2name, key2uri, reserved_names = {}, {}, set()
        for acc, link in assigned.items():
            try:
                pname = gmc.parse_vless(link, acc)["name"]
            except ValueError:
                pname = acc
            k = rot.node_key(link)
            key2name[k], key2uri[k] = pname, link
            reserved_names.add(pname)
        nf = Path(self.rc["nodes_file"])
        pool = rot.load_pool(nf if nf.is_absolute() else ROOT / nf)
        names = rot.pool_names(pool, reserved_names, set(key2name))
        for n in pool:
            if n.key in names:
                key2name[n.key], key2uri[n.key] = names[n.key], n.uri
        guest = set(self.gcfg["guest_accounts"])
        self.env = SimpleNamespace(assigned=assigned, key2name=key2name, key2uri=key2uri)
        # ноды логин-аккаунтов (не гостевых) — только им, гостям не отдаём
        self.reserved_keys = {rot.node_key(l) for a, l in assigned.items() if a not in guest}

    def guest_names(self) -> list[str]:
        return sorted(self.gcfg["guest_accounts"], key=rot._acc_num)

    # ---- реестр ----
    def _make_identity(self, key: str, uri: str, fp: dict, geo: dict) -> dict:
        return {
            "id": ident_id(key), "node_key": key, "node_uri": uri, "fp": fp, "geo": geo,
            "created": time.time(), "banned_at": None, "ban_reason": None,
            "active_since": None, "ok_at": None, "uses": 0,
        }

    def ensure_seeds(self) -> None:
        """Каждому гостевому слоту с нодой в vless_accounts.env — стартовая
        идентичность (из accounts.yaml / guest yaml). Нужно, чтобы чужие
        стартовые ноды считались занятыми."""
        changed = False
        for name in self.guest_names():
            slot = self.st["slots"].get(name)
            if slot and slot.get("active") in self.st["identities"]:
                continue
            link = self.env.assigned.get(name)
            acc = self.accounts.get(name)
            if not link or not acc:
                continue
            key = rot.node_key(link)
            iid = ident_id(key)
            if iid not in self.st["identities"]:
                spec = self.gcfg["guest_accounts"].get(name) or {}
                imp = acc.get("impersonate") or spec.get("impersonate")
                inferred = rgc.infer_profile(imp)
                fp = {
                    "engine": spec.get("engine") or inferred["engine"],
                    "channel": spec["channel"] if "channel" in spec else inferred["channel"],
                    "device": spec.get("device") or inferred["device"],
                    "user_agent": acc.get("user_agent") or spec.get("user_agent"),
                    "impersonate": imp, "label": "seed",
                }
                node_name = self.env.key2name.get(key, name)
                geo = (self.gcfg.get("account_geo", {}).get(name) or spec.get("geo")
                       or rot.geo_for(node_name))
                self.st["identities"][iid] = self._make_identity(key, link, fp, geo)
            self.st["slots"].setdefault(name, {"rotations": [], "rev": 0})["active"] = iid
            changed = True
        if changed:
            save_state(self.st)

    # ---- mihomo ----
    def reconcile(self) -> None:
        """Выбор ноды в mihomo = активная идентичность слота (после
        рестарта mihomo select сбрасывается на первую ноду группы)."""
        for name, slot in self.st["slots"].items():
            acc = self.accounts.get(name)
            ident = self.st["identities"].get(slot.get("active"))
            if not acc or not ident or not acc.get("proxy_port"):
                continue
            node = self.env.key2name.get(ident["node_key"])
            if not node:
                continue
            group = f"pg-{name}"
            try:
                info = self.mihomo._req("GET", "/proxies/" + urllib.parse.quote(group, safe="")) or {}
                if info.get("now") != node:
                    log.warning("[%s] mihomo на %r, а должна быть %r — восстанавливаю",
                                name, info.get("now"), node)
                    self.mihomo.select(group, node)
                    self.mihomo.close_inbound(int(acc["proxy_port"]))
            except rot.RotationError as e:
                if e.fatal:
                    log.warning("mihomo API недоступен — сверку пропускаю: %s", e)
                    return
                log.warning("[%s] сверка mihomo: %s", name, e)

    # ---- заявки ----
    def pending_requests(self) -> list[tuple[str, dict]]:
        out = []
        for p in idn.REQUESTS_DIR.glob("*.json"):
            r = idn.read_json(p)
            if r and r.get("status") in ("pending", "working"):
                out.append((p.stem, r))
        out.sort(key=lambda x: x[1].get("ts", 0))
        return out

    def set_req(self, name: str, req: dict, **kw) -> None:
        req.update(kw)
        idn.write_json_atomic(idn.request_path(name), req)

    # ---- выбор кандидатов ----
    def _bad(self, key: str) -> bool:
        b = self.st["bad_nodes"].get(key)
        return bool(b) and time.time() - b["ts"] < b.get("ttl_hours", 24) * 3600

    def candidates(self, name: str, tried: set[str]) -> list[tuple[str, str]]:
        now = time.time()
        cd = self.rc["ban_cooldown_hours"] * 3600
        st = self.st
        active = st["slots"][name].get("active")
        leased = {s.get("active") for n, s in st["slots"].items() if n != name}
        known = {i["node_key"] for i in st["identities"].values()}

        def usable(key: str) -> bool:
            return (key in self.env.key2name and key not in self.reserved_keys
                    and key not in tried and not self._bad(key))

        virgin = [k for k in self.env.key2name if k not in known and usable(k)]
        random.shuffle(virgin)
        rested = sorted(
            (i for i in st["identities"].values()
             if i["id"] not in leased and i["id"] != active and usable(i["node_key"])
             and (i.get("banned_at") is None or now - i["banned_at"] >= cd)),
            key=lambda i: i.get("banned_at") or 0,
        )
        return [("virgin", k) for k in virgin] + [("rested", i["id"]) for i in rested]

    def next_eligible_at(self, name: str) -> float:
        now = time.time()
        cd = self.rc["ban_cooldown_hours"] * 3600
        leased = {s.get("active") for n, s in self.st["slots"].items() if n != name}
        active = self.st["slots"][name].get("active")
        ts = [i["banned_at"] + cd for i in self.st["identities"].values()
              if i["id"] not in leased and i["id"] != active and i.get("banned_at")
              and i["node_key"] in self.env.key2name and i["node_key"] not in self.reserved_keys]
        return min(ts) if ts else now + self.rc["failed_all_pause_seconds"]

    def new_fingerprint(self, exclude: set[str]) -> dict:
        used = ({a.get("impersonate") for a in self.accounts.values()}
                | {i["fp"]["impersonate"] for i in self.st["identities"].values()} | exclude)
        pool = [t for t in rf.build_pool(self.fp_args) if not (t.family == "safari" and t.major > 30)]
        free = [t for t in pool if t.name not in used] or pool
        t = random.choice(free)
        p = rf.build_profile(t, random)
        return {"engine": p.engine, "channel": p.channel, "device": p.device,
                "user_agent": p.user_agent, "impersonate": p.impersonate, "label": p.label}

    # ---- одна попытка: нода -> precheck -> харвест ----
    def try_candidate(self, name: str, kind: str, ref: str, tried_imps: set[str]) -> bool:
        acc = self.accounts[name]
        port = int(acc["proxy_port"])
        group = f"pg-{name}"

        if kind == "virgin":
            key = ref
            node_name = self.env.key2name[key]
            fp = self.new_fingerprint(tried_imps)
            ident = self._make_identity(key, self.env.key2uri[key], fp, rot.geo_for(node_name))
        else:
            ident = self.st["identities"][ref]
            key = ident["node_key"]
            node_name = self.env.key2name[key]
            fp = ident["fp"]
        tried_imps.add(fp["impersonate"])
        log.info("[%s] кандидат (%s): нода %s, отпечаток %s", name, kind, node_name, fp["impersonate"])

        try:
            self.mihomo.select(group, node_name)
        except rot.RotationError as e:
            log.warning("[%s] mihomo не принял ноду %s (%s) — пул не в конфиге? "
                        "перегенерируй mihomo_config.yaml и перезапусти mihomo", name, node_name, e)
            return False
        self.mihomo.close_inbound(port)
        time.sleep(0.5)

        alive, info = rot.precheck(port)
        if not alive:
            self.st["bad_nodes"][key] = {"ts": time.time(), "ttl_hours": self.rc["bad_node_ttl_hours"],
                                         "reason": info[:100], "name": node_name}
            save_state(self.st)
            log.warning("[%s] нода %s не отвечает (%s) — в bad_nodes на %dч",
                        name, node_name, info, self.rc["bad_node_ttl_hours"])
            return False
        log.info("[%s] нода жива (%s)", name, info)

        staging_rel = f"{STAGING_REL}/{name}.json"
        staging = ROOT / staging_rel
        view = {"name": name, "cookie_file": staging_rel, "proxy_port": port,
                "impersonate": fp["impersonate"], "user_agent": fp["user_agent"]}
        cfg = copy.deepcopy(self.gcfg)
        spec = {"engine": fp["engine"], "channel": fp.get("channel"),
                "user_agent": fp["user_agent"], "impersonate": fp["impersonate"]}
        if fp.get("device"):
            spec["device"] = fp["device"]
        cfg["guest_accounts"][name] = spec
        cfg.setdefault("account_geo", {})[name] = ident["geo"]

        try:
            with sync_playwright() as pw:
                res = rgc.process_guest_account(pw, view, cfg, False)
        except Exception as e:
            log.exception("[%s] харвест упал", name)
            res = SimpleNamespace(ok=False, detail=str(e))

        if not res.ok:
            log.warning("[%s] харвест/верификация не прошли: %s", name, res.detail)
            if staging.exists():
                staging.unlink()
            if kind == "rested":
                ident["banned_at"] = time.time()
                ident["ban_reason"] = "harvest failed"
            else:
                # целинная нода не прошла верификацию: не берём её час, чтобы
                # не молотить одну и ту же в каждой заявке
                self.st["bad_nodes"][key] = {"ts": time.time(), "ttl_hours": 1,
                                             "reason": "harvest: " + str(res.detail)[:80], "name": node_name}
            save_state(self.st)
            return False

        self.commit(name, ident, node_name, fp, staging, acc)
        return True

    def commit(self, name: str, ident: dict, node_name: str, fp: dict, staging: Path, acc: dict) -> None:
        now = time.time()
        ident.update(banned_at=None, ban_reason=None, active_since=now, ok_at=now,
                     uses=ident.get("uses", 0) + 1)
        self.st["identities"][ident["id"]] = ident
        slot = self.st["slots"][name]
        slot["active"] = ident["id"]
        slot.setdefault("rotations", []).append(now)
        slot["rev"] = slot.get("rev", 0) + 1
        save_state(self.st)

        # 1) профиль, 2) cookies — воркер читает в таком же порядке
        idn.write_json_atomic(idn.slot_profile_path(name), {
            "rev": slot["rev"], "identity": ident["id"], "node": node_name,
            "engine": fp["engine"], "channel": fp.get("channel"), "device": fp.get("device"),
            "user_agent": fp["user_agent"], "impersonate": fp["impersonate"],
            "geo": ident["geo"], "updated": now,
        })
        os.replace(staging, ROOT / acc["cookie_file"])
        log.info("[%s] ротация СОХРАНЕНА: нода %s, %s (rev %d)", name, node_name, fp["impersonate"], slot["rev"])

    # ---- заявка целиком ----
    def rotate(self, name: str) -> None:
        now = time.time()
        rc = self.rc
        slot = self.st["slots"][name]
        prev = self.st["identities"][slot["active"]]
        acc = self.accounts[name]

        if prev.get("active_since"):
            life = now - prev["active_since"]
            self.st["ban_lifetimes"].append({"slot": name, "identity": prev["id"], "seconds": life, "ts": now})
            log.info("[%s] идентичность %s (%s) проработала %.0f мин до пустого листинга",
                     name, prev["id"], node_label(prev["node_uri"]), life / 60)
        prev["banned_at"], prev["ban_reason"] = now, "empty_listing"
        save_state(self.st)

        tried: set[str] = set()
        tried_imps: set[str] = set()
        attempts = 0
        while attempts < rc["max_candidates_per_request"]:
            cands = self.candidates(name, tried)
            if not cands:
                break
            kind, ref = cands[0]
            key = ref if kind == "virgin" else self.st["identities"][ref]["node_key"]
            tried.add(key)
            attempts += 1
            if self.try_candidate(name, kind, ref, tried_imps):
                return

        # не вышло — вернуть mihomo на прежнюю (забаненную) ноду
        prev_node = self.env.key2name.get(prev["node_key"])
        if prev_node:
            try:
                self.mihomo.select(f"pg-{name}", prev_node)
                self.mihomo.close_inbound(int(acc["proxy_port"]))
            except rot.RotationError as e:
                log.warning("[%s] не вернул ноду %s: %s", name, prev_node, e)
        when = self.next_eligible_at(name)
        raise Reject(f"нет рабочих кандидатов (попыток {attempts})",
                     max(when, time.time() + rc["failed_all_pause_seconds"] if attempts else when))

    def handle(self, name: str, req: dict) -> None:
        rc = self.rc
        now = time.time()
        if name not in rc["slots"] or name not in self.gcfg["guest_accounts"]:
            raise Reject("слот не в rotation.slots / guest_accounts", now + 3600)
        if name not in self.st["slots"]:
            raise Reject("у слота нет ноды в vless_accounts.env", now + 3600)

        rots = [t for t in self.st["slots"][name].get("rotations", []) if now - t < 86400]
        if len(rots) >= rc["max_rotations_per_day"]:
            raise Reject(f"лимит {rc['max_rotations_per_day']} ротаций в сутки", min(rots) + 86400)

        n_en = sum(1 for n in rc["slots"] if (self.accounts.get(n) or {}).get("enabled"))
        pend = len(self.pending_requests())
        if n_en >= 3 and pend >= max(2, math.ceil(rc["global_outage_fraction"] * n_en)):
            raise Reject(f"пусто у {pend}/{n_en} слотов сразу — похоже на сбой Reddit/сети, а не бан",
                         now + rc["global_outage_pause_seconds"])

        with BrowserLock(rc["lock_timeout_seconds"]):
            self.refresh_ctx()  # пока ждали lock, файлы могли измениться
            self.set_req(name, req, status="working", started=time.time())
            self.rotate(name)

    # ---- главный цикл ----
    def run(self, once: bool = False) -> None:
        idn.REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        idn.SLOTS_DIR.mkdir(parents=True, exist_ok=True)
        for d in (idn.STATE_DIR, idn.REQUESTS_DIR, idn.SLOTS_DIR):
            try:
                os.chmod(d, 0o777)
            except OSError:
                pass

        # зависшие после падения демона "working" -> снова "pending"
        for p in idn.REQUESTS_DIR.glob("*.json"):
            r = idn.read_json(p)
            if r and r.get("status") == "working":
                r["status"] = "pending"
                idn.write_json_atomic(p, r)

        last_reconcile = 0.0
        log.info("rotation_daemon: старт")
        while True:
            try:
                self.refresh_ctx()
                if not self.rc["enabled"]:
                    time.sleep(10)
                    continue
                self.ensure_seeds()
                if time.monotonic() - last_reconcile >= self.rc["reconcile_seconds"]:
                    self.reconcile()
                    last_reconcile = time.monotonic()

                for name, req in self.pending_requests()[:1]:
                    log.info("[%s] заявка на ротацию (%s, пусто подряд: %s)",
                             name, req.get("reason"), req.get("empty_streak"))
                    try:
                        self.handle(name, req)
                        self.set_req(name, req, status="done",
                                     retry_after=time.time() + self.rc["request_cooldown_seconds"])
                    except Reject as e:
                        log.warning("[%s] заявка отклонена: %s (повтор не раньше %s)", name, e.reason,
                                    time.strftime("%H:%M:%S", time.localtime(e.retry_after)))
                        self.set_req(name, req, status="rejected", reason=e.reason, retry_after=e.retry_after)
                    except Exception as e:
                        log.exception("[%s] ошибка обработки заявки", name)
                        self.set_req(name, req, status="rejected", reason=f"ошибка демона: {e}"[:200],
                                     retry_after=time.time() + 300)
            except Exception:
                log.exception("ошибка итерации демона")
            if once:
                return
            time.sleep(self.rc["poll_seconds"])


# ---------------------------------------------------------------- #

def cmd_status() -> int:
    st = load_state()
    now = time.time()
    print("СЛОТЫ")
    for n in sorted(st["slots"], key=rot._acc_num):
        s = st["slots"][n]
        i = st["identities"].get(s.get("active"), {})
        r24 = sum(1 for t in s.get("rotations", []) if now - t < 86400)
        print(f"  {n:11} {node_label(i.get('node_uri', '?')):22} {i.get('fp', {}).get('impersonate', '?'):18} "
              f"ротаций/24ч={r24} rev={s.get('rev', 0)}")
    print("\nИДЕНТИЧНОСТИ")
    leased = {s.get("active") for s in st["slots"].values()}
    for i in sorted(st["identities"].values(), key=lambda x: x.get("banned_at") or 0):
        b = i.get("banned_at")
        state = "АКТИВНА" if i["id"] in leased else ("свободна" if not b else f"бан {(now - b) / 3600:.1f}ч назад")
        print(f"  {i['id']} {node_label(i['node_uri']):22} {i['fp']['impersonate']:18} {state} использований={i.get('uses', 0)}")
    bad = {k: v for k, v in st["bad_nodes"].items() if now - v["ts"] < v.get("ttl_hours", 24) * 3600}
    print(f"\nМЁРТВЫЕ НОДЫ: {len(bad)}")
    for v in bad.values():
        print(f"  {v.get('name')}: {v.get('reason')}")
    lives = st["ban_lifetimes"][-10:]
    if lives:
        print("\nСКОЛЬКО ПРОЖИЛА ИДЕНТИЧНОСТЬ ДО БАНА (последние)")
        for l in lives:
            print(f"  {l['slot']}: {l['seconds'] / 60:.0f} мин")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-headless", action="store_true", help="окно браузера (под xvfb-run)")
    ap.add_argument("--once", action="store_true", help="одна итерация и выход")
    ap.add_argument("--status", action="store_true", help="показать реестр и выйти")
    args = ap.parse_args()
    if args.status:
        return cmd_status()
    Daemon(args).run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
