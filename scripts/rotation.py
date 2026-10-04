#!/usr/bin/env python3
"""
scripts/rotation.py — автоматическая смена ноды (IP) и отпечатка
(impersonate + UA + engine + гео) для гостевых слотов.

Сам по себе не запускается как джоб — его используют:
  - scripts/refresh_guest_cookies_auto.py  (класс Rotator),
  - scripts/generate_mihomo_config.py      (функция extend_with_pool),
а из консоли — только для просмотра состояния:

    python3 scripts/rotation.py            # пул, плохие ноды, история

Как устроена смена ноды: в mihomo_config.yaml каждая группа pg-account_N
(type: select) содержит назначенную ноду ПЕРВОЙ и дальше весь пул из
all_nodes.txt. Переключение — PUT /proxies/pg-account_N у external-
controller mihomo, без рестарта. Порт аккаунта (proxy_port) не меняется.

Сохранение (accounts.yaml / refresh_guest_cookies.yaml / vless_accounts.env)
происходит ТОЛЬКО после успешной верификации новых cookies. Если все
попытки провалились — выбор в mihomo возвращается на прежнюю ноду, файлы
не трогаются.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import generate_mihomo_config as gmc  # noqa: E402
import rotate_fingerprints as rf  # noqa: E402

log = logging.getLogger("scraper")

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
ACCOUNTS_PATH = ROOT / "accounts.yaml"
GUEST_PATH = SCRIPTS / "refresh_guest_cookies.yaml"
ENV_PATH = gmc.VLESS_ENV_PATH
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "rotation_state.json"

DEFAULT_OPTS = {
    "enabled": True,
    "nodes_file": "all_nodes.txt",
    "mihomo_api": "http://127.0.0.1:9090",
    "mihomo_secret": "",
    "rotate_node": True,
    "rotate_fingerprint": True,
    "max_rotations": 3,               # попыток ротации на аккаунт за прогон
    "plain_retries": 1,               # повтор БЕЗ смены перед ротацией
    "plain_retry_delay_seconds": 20,
    "precheck_candidates": 6,         # сколько нод пробовать за одну ротацию
    "bad_node_ttl_hours": 24,
    "max_rotations_per_day": 4,       # успешных смен на аккаунт в сутки
    "breaker_after": 3,               # подряд провальных ротаций -> стоп до конца прогона
    "allow_old": False,
    "no_edge": True,                  # Edge требует установленный channel msedge
    "no_mobile": False,
}

# код страны из имени ноды ("us74.trust.zone" -> us) -> (locale, timezone)
COUNTRY_GEO = {
    "us": ("en-US", "America/New_York"), "ca": ("en-CA", "America/Toronto"),
    "uk": ("en-GB", "Europe/London"), "gb": ("en-GB", "Europe/London"),
    "ie": ("en-IE", "Europe/Dublin"), "de": ("de-DE", "Europe/Berlin"),
    "fr": ("fr-FR", "Europe/Paris"), "nl": ("nl-NL", "Europe/Amsterdam"),
    "be": ("nl-BE", "Europe/Brussels"), "ch": ("de-CH", "Europe/Zurich"),
    "at": ("de-AT", "Europe/Vienna"), "se": ("sv-SE", "Europe/Stockholm"),
    "no": ("nb-NO", "Europe/Oslo"), "dk": ("da-DK", "Europe/Copenhagen"),
    "fi": ("fi-FI", "Europe/Helsinki"), "pl": ("pl-PL", "Europe/Warsaw"),
    "cz": ("cs-CZ", "Europe/Prague"), "sk": ("sk-SK", "Europe/Bratislava"),
    "hu": ("hu-HU", "Europe/Budapest"), "ro": ("ro-RO", "Europe/Bucharest"),
    "bg": ("bg-BG", "Europe/Sofia"), "rs": ("sr-RS", "Europe/Belgrade"),
    "gr": ("el-GR", "Europe/Athens"), "tr": ("tr-TR", "Europe/Istanbul"),
    "ua": ("uk-UA", "Europe/Kiev"), "lv": ("lv-LV", "Europe/Riga"),
    "lt": ("lt-LT", "Europe/Vilnius"), "ee": ("et-EE", "Europe/Tallinn"),
    "es": ("es-ES", "Europe/Madrid"), "pt": ("pt-PT", "Europe/Lisbon"),
    "it": ("it-IT", "Europe/Rome"), "al": ("sq-AL", "Europe/Tirane"),
    "il": ("he-IL", "Asia/Jerusalem"), "in": ("en-IN", "Asia/Kolkata"),
    "jp": ("ja-JP", "Asia/Tokyo"), "sg": ("en-SG", "Asia/Singapore"),
    "au": ("en-AU", "Australia/Sydney"), "nz": ("en-NZ", "Pacific/Auckland"),
    "br": ("pt-BR", "America/Sao_Paulo"), "mx": ("es-MX", "America/Mexico_City"),
}


class RotationError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


# ---------------------------------------------------------------- #
#  Узлы и пул
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class Node:
    key: str    # ссылка без #fragment — идентичность ноды
    name: str   # имя прокси в mihomo
    uri: str


def node_key(uri: str) -> str:
    return uri.split("#", 1)[0].strip()


def geo_for(node_name: str) -> dict:
    m = re.match(r"([a-z]{2})\d", node_name.strip().lower())
    loc, tz = COUNTRY_GEO.get(m.group(1) if m else "", ("en-US", "America/New_York"))
    return {"locale": loc, "timezone_id": tz}


def load_pool(path: Path) -> list[Node]:
    """Любой текст, содержащий vless://-ссылки (по одной в строке, можно с
    префиксом 'name = '), либо base64-подписка целиком."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    uris = re.findall(r"vless://[^\s\"']+", text)
    if not uris:
        try:
            raw = text.strip()
            dec = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
            uris = re.findall(r"vless://[^\s\"']+", dec)
        except Exception:
            uris = []
    out, seen = [], set()
    for u in uris:
        k = node_key(u)
        if k in seen:
            continue
        seen.add(k)
        name = urllib.parse.unquote(urllib.parse.urlsplit(u).fragment).strip() or k[-24:]
        out.append(Node(k, name, u))
    return out


def pool_names(pool: list[Node], reserved: set[str], assigned_keys: set[str]) -> dict[str, str]:
    """key -> уникальное имя прокси в mihomo. Ноды, уже назначенные
    аккаунтам, пропускаются (у них своё имя из vless_accounts.env)."""
    used, out = set(reserved), {}
    for n in pool:
        if n.key in assigned_keys:
            continue
        name, i = n.name, 2
        while name in used:
            name = f"{n.name}~{i}"
            i += 1
        used.add(name)
        out[n.key] = name
    return out


def extend_with_pool(config: dict, nodes_file: Path | None = None) -> int:
    """Вызывается из generate_mihomo_config.main(): добавляет пул нод в
    proxies и в КАЖДУЮ группу pg-account_N (после назначенной ноды)."""
    pool = load_pool(nodes_file or (ROOT / DEFAULT_OPTS["nodes_file"]))
    if not pool:
        return 0
    links = gmc.load_vless_links()
    assigned_keys = {node_key(l) for l in links.values()}
    names = pool_names(pool, {p["name"] for p in config["proxies"]}, assigned_keys)
    added = []
    for n in pool:
        name = names.get(n.key)
        if not name:
            continue
        try:
            proxy = gmc.parse_vless(n.uri, fallback_name=name)
        except ValueError as e:
            print(f"ПРЕДУПРЕЖДЕНИЕ: нода пула пропущена ({e})", file=sys.stderr)
            continue
        proxy["name"] = name
        config["proxies"].append(proxy)
        added.append(name)
    for g in config["proxy-groups"]:
        g["proxies"] = g["proxies"] + added
    return len(added)


# ---------------------------------------------------------------- #
#  Состояние и файлы
# ---------------------------------------------------------------- #

def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("bad", {})
    st.setdefault("history", [])
    return st


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st["history"] = st["history"][-300:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_inplace(path: Path, text: str) -> None:
    """Для accounts.yaml: файл примонтирован в контейнер как ОДИН файл
    (bind mount) — os.replace() сменил бы inode, и контейнер продолжил бы
    видеть старый. Поэтому пишем в тот же inode."""
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def _acc_num(name: str) -> int:
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def profile_entry(p) -> dict:
    e = {"engine": p.engine, "channel": p.channel}
    if p.device:
        e["device"] = p.device
    e["user_agent"] = p.user_agent
    e["impersonate"] = p.impersonate
    return e


def update_guest_yaml(account: str, profile, geo: dict | None) -> None:
    text = GUEST_PATH.read_text(encoding="utf-8")
    m = re.search(r"(?m)^guest_accounts:[ \t]*\n", text)
    if not m:
        raise RotationError("в refresh_guest_cookies.yaml нет строки 'guest_accounts:'")
    head = text[:m.start()]
    guest = (yaml.safe_load(text) or {}).get("guest_accounts") or {}
    entry = dict(guest.get(account) or {})
    if profile is not None:
        old_geo = entry.get("geo")
        entry = profile_entry(profile)
        if old_geo:
            entry["geo"] = old_geo
    if geo:
        entry["geo"] = geo
    guest[account] = entry
    ordered = {k: guest[k] for k in sorted(guest, key=_acc_num)}
    tail = yaml.safe_dump({"guest_accounts": ordered}, allow_unicode=True, sort_keys=False, width=200)
    _atomic_write(GUEST_PATH, head + tail)


def update_env(account: str, uri: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    pat = re.compile(rf"(?m)^\s*{re.escape(account)}\s*=.*$")
    line = f"{account} = {uri}"
    new = pat.sub(line, text, count=1) if pat.search(text) else text.rstrip("\n") + "\n" + line + "\n"
    _atomic_write(ENV_PATH, new)


class JobLock:
    """Один прогон за раз (два браузера на слабом хосте = OOM, плюс гонка
    за state/ и mihomo)."""

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(STATE_DIR / "job.lock", "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Другой прогон refresh_guest_cookies уже идёт (state/job.lock) — выхожу.")
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
        except Exception:
            pass


# ---------------------------------------------------------------- #
#  mihomo API и проверка живости
# ---------------------------------------------------------------- #

class Mihomo:
    def __init__(self, base: str, secret: str = ""):
        self.base = base.rstrip("/")
        self.secret = secret

    def _req(self, method: str, path: str, body=None, timeout: float = 5):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.secret:
            req.add_header("Authorization", f"Bearer {self.secret}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RotationError(f"mihomo API {method} {path}: HTTP {e.code} {e.read()[:100]!r}")
        except Exception as e:
            raise RotationError(f"mihomo API недоступен ({self.base}): {e}", fatal=True)
        return json.loads(raw) if raw else None

    def select(self, group: str, node: str) -> None:
        self._req("PUT", f"/proxies/{urllib.parse.quote(group, safe='')}", {"name": node})

    def close_inbound(self, port: int) -> None:
        """Закрыть живые keep-alive соединения порта, чтобы воркер не
        продолжал ходить через старую ноду."""
        try:
            d = self._req("GET", "/connections") or {}
            for c in d.get("connections", []):
                if str((c.get("metadata") or {}).get("inboundPort")) == str(port):
                    try:
                        self._req("DELETE", f"/connections/{c['id']}")
                    except RotationError:
                        pass
        except RotationError:
            pass


def precheck(port: int, timeout: float = 12) -> tuple[bool, str]:
    """Нода жива, если через неё открывается нейтральный сайт и Reddit
    отвечает ЛЮБЫМ http-кодом (403 на главную без cookies — не критерий)."""
    from curl_cffi import requests as cffi
    px = {"http": f"http://{PROXY_HOST}:{port}", "https": f"http://{PROXY_HOST}:{port}"}
    try:
        r = cffi.get("https://www.cloudflare.com/cdn-cgi/trace", impersonate="chrome131",
                     proxies=px, timeout=timeout)
        if r.status_code != 200:
            return False, f"нейтральный сайт: {r.status_code}"
        ip = next((l[3:] for l in r.text.splitlines() if l.startswith("ip=")), "?")
        r2 = cffi.get("https://www.reddit.com/", impersonate="chrome131", proxies=px, timeout=timeout + 5)
        return True, f"ip={ip} reddit={r2.status_code}"
    except Exception as e:
        return False, str(e)[:90]


# ---------------------------------------------------------------- #
#  Ротатор
# ---------------------------------------------------------------- #

@dataclass
class Session:
    account: str
    port: int
    group: str
    cfg: dict
    orig_node: Node | None
    active_node: Node | None
    orig_spec: dict | None
    orig_geo: dict | None
    new_node: Node | None = None
    new_profile: object | None = None
    tried_nodes: set = field(default_factory=set)
    tried_imps: set = field(default_factory=set)
    bad_engines: set = field(default_factory=set)

    def summary(self) -> str:
        parts = []
        if self.new_node:
            parts.append(f"нода {self.orig_node.name if self.orig_node else '?'} -> {self.new_node.name}")
        if self.new_profile:
            parts.append(f"отпечаток -> {self.new_profile.impersonate} ({self.new_profile.label})")
        return "; ".join(parts) or "без изменений"


class Rotator:
    def __init__(self, opts: dict, dry_run: bool = False):
        self.o = {**DEFAULT_OPTS, **(opts or {})}
        self.dry_run = dry_run
        self.state = load_state()
        ttl = self.o["bad_node_ttl_hours"] * 3600
        now = time.time()
        self.state["bad"] = {k: v for k, v in self.state["bad"].items() if now - v.get("ts", 0) < ttl}

        nf = Path(self.o["nodes_file"])
        self.pool = load_pool(nf if nf.is_absolute() else ROOT / nf)
        if not self.pool:
            log.warning("[rotation] пул нод пуст (%s) — смена IP невозможна, только отпечатки", nf)
        self.assigned = gmc.load_vless_links()
        self.accounts = (yaml.safe_load(ACCOUNTS_PATH.read_text(encoding="utf-8")) or {}).get("accounts", [])
        self.mihomo = Mihomo(self.o["mihomo_api"], self.o.get("mihomo_secret", ""))

        reserved = set()
        for acc, link in self.assigned.items():
            try:
                reserved.add(gmc.parse_vless(link, acc)["name"])
            except ValueError:
                reserved.add(acc)
        self.names = pool_names(self.pool, reserved, {node_key(l) for l in self.assigned.values()})
        self.fp_args = SimpleNamespace(allow_old=self.o["allow_old"], no_edge=self.o["no_edge"],
                                       no_mobile=self.o["no_mobile"])
        self._backed_up: set[Path] = set()

    # ---- служебное ----
    def _is_bad(self, key: str) -> bool:
        return key in self.state["bad"]

    def _mark_bad(self, node: Node | None, account: str, reason: str) -> None:
        if node is None:
            return
        self.state["bad"][node.key] = {"ts": time.time(), "name": node.name, "account": account,
                                       "reason": reason[:120]}
        save_state(self.state)
        log.warning("[%s] нода %s помечена плохой на %dч: %s", account, node.name,
                    self.o["bad_node_ttl_hours"], reason[:80])

    def _rotations_today(self, account: str) -> int:
        since = time.time() - 86400
        return sum(1 for h in self.state["history"] if h.get("account") == account and h.get("ts", 0) > since)

    def _backup_once(self, path: Path) -> None:
        if path in self._backed_up or not path.exists():
            return
        shutil.copy2(path, path.with_name(path.name + ".bak-rotation"))
        self._backed_up.add(path)

    # ---- сессия на аккаунт ----
    def begin(self, account: dict, cfg: dict) -> Session:
        name = account["name"]
        link = self.assigned.get(name)
        orig = None
        if link:
            try:
                pname = gmc.parse_vless(link, name)["name"]
            except ValueError:
                pname = name
            orig = Node(node_key(link), pname, link)
        sess = Session(
            account=name, port=int(account["proxy_port"]), group=f"pg-{name}", cfg=cfg,
            orig_node=orig, active_node=orig,
            orig_spec=copy.deepcopy(cfg["guest_accounts"].get(name)),
            orig_geo=copy.deepcopy(cfg.get("account_geo", {}).get(name)),
        )
        self._restore_selection(sess)  # самолечение, если прошлый прогон оборвался посреди попытки
        return sess

    def _restore_selection(self, sess: Session) -> None:
        if sess.orig_node is None:
            return
        try:
            self.mihomo.select(sess.group, sess.orig_node.name)
            self.mihomo.close_inbound(sess.port)
        except RotationError as e:
            log.warning("[%s] не удалось вернуть выбор ноды в mihomo: %s", sess.account, e)

    # ---- подбор ----
    def _next_node(self, sess: Session) -> Node | None:
        cands = [n for n in self.pool
                 if n.key in self.names and not self._is_bad(n.key) and n.key not in sess.tried_nodes
                 and (sess.orig_node is None or n.key != sess.orig_node.key)]
        random.shuffle(cands)
        if not cands:
            log.warning("[%s] в пуле не осталось подходящих нод (плохие/уже пробованные/назначенные)", sess.account)
            return None
        for n in cands[: self.o["precheck_candidates"]]:
            sess.tried_nodes.add(n.key)
            mname = self.names[n.key]
            try:
                self.mihomo.select(sess.group, mname)
            except RotationError as e:
                if e.fatal:
                    log.error("[%s] %s", sess.account, e)
                    return None
                log.warning("[%s] mihomo не принял ноду %s (%s) — пул не загружен в конфиг? "
                            "см. rotation_setup.md, шаг 3", sess.account, mname, e)
                continue
            self.mihomo.close_inbound(sess.port)
            time.sleep(0.5)
            alive, info = precheck(sess.port)
            if alive:
                log.info("[%s] кандидат %s жив (%s)", sess.account, mname, info)
                return Node(n.key, mname, n.uri)
            self._mark_bad(n, sess.account, "precheck: " + info)
        self._restore_selection(sess)
        return None

    def _next_profile(self, sess: Session):
        used = {a.get("impersonate") for a in self.accounts if a["name"] != sess.account} | sess.tried_imps
        targets = [t for t in rf.build_pool(self.fp_args)
                   if t.name not in used and not (t.family == "safari" and t.major > 30)]
        random.shuffle(targets)
        for t in targets:
            p = rf.build_profile(t, random)
            if (p.channel or p.engine) in sess.bad_engines:
                continue
            sess.tried_imps.add(t.name)
            return p
        log.warning("[%s] не осталось свободных impersonate-таргетов", sess.account)
        return None

    # ---- публичное API ----
    def rotate(self, sess: Session, mode: str, kind: str, detail: str) -> bool:
        """mode: node | fingerprint | both. Меняет только in-memory cfg и
        выбор ноды в mihomo; на диск ничего не пишет до commit()."""
        if self._rotations_today(sess.account) >= self.o["max_rotations_per_day"]:
            log.warning("[%s] лимит %d смен в сутки исчерпан — не ротирую", sess.account,
                        self.o["max_rotations_per_day"])
            return False

        if kind == "network" and sess.active_node is not None:
            alive, info = precheck(sess.port)
            if not alive:
                self._mark_bad(sess.active_node, sess.account, info)
        if kind == "engine":
            spec = sess.cfg["guest_accounts"].get(sess.account) or {}
            sess.bad_engines.add(spec.get("channel") or spec.get("engine"))

        changed = False
        if mode in ("node", "both"):
            n = self._next_node(sess)
            if n:
                sess.new_node = sess.active_node = n
                changed = True
            elif mode == "node":
                return False
        if mode in ("fingerprint", "both"):
            p = self._next_profile(sess)
            if p:
                sess.new_profile = p
                changed = True
            elif mode == "fingerprint":
                return False
        if not changed:
            return False

        if sess.new_profile:
            sess.cfg["guest_accounts"][sess.account] = profile_entry(sess.new_profile)
        if sess.new_node:
            sess.cfg.setdefault("account_geo", {})[sess.account] = geo_for(sess.new_node.name)
        log.info("[%s] ротация (%s): %s", sess.account, mode, sess.summary())
        return True

    def commit(self, sess: Session) -> None:
        if self.dry_run:
            log.info("[%s] dry-run: ротация НЕ сохранена (%s)", sess.account, sess.summary())
            self.abort(sess)
            return
        n = sess.account
        if sess.new_profile:
            self._backup_once(ACCOUNTS_PATH)
            text = ACCOUNTS_PATH.read_text(encoding="utf-8")
            _write_inplace(ACCOUNTS_PATH, rf.patch_accounts_text(text, {n: sess.new_profile}))
            for a in self.accounts:
                if a["name"] == n:
                    a["impersonate"] = sess.new_profile.impersonate
        self._backup_once(GUEST_PATH)
        geo = sess.cfg.get("account_geo", {}).get(n) if sess.new_node else None
        update_guest_yaml(n, sess.new_profile, geo)
        if sess.new_node:
            self._backup_once(ENV_PATH)
            update_env(n, sess.new_node.uri)
            self.assigned[n] = sess.new_node.uri
        self.state["history"].append({
            "ts": time.time(), "account": n, "summary": sess.summary(),
            "node": sess.new_node.name if sess.new_node else None,
            "impersonate": sess.new_profile.impersonate if sess.new_profile else None,
        })
        save_state(self.state)
        self.mihomo.close_inbound(sess.port)
        log.info("[%s] ротация СОХРАНЕНА: %s", n, sess.summary())

    def abort(self, sess: Session) -> None:
        self._restore_selection(sess)
        n = sess.account
        if sess.orig_spec is not None:
            sess.cfg["guest_accounts"][n] = sess.orig_spec
        if sess.orig_geo is None:
            sess.cfg.get("account_geo", {}).pop(n, None)
        else:
            sess.cfg.setdefault("account_geo", {})[n] = sess.orig_geo


# ---------------------------------------------------------------- #

if __name__ == "__main__":
    st = load_state()
    pool = load_pool(ROOT / DEFAULT_OPTS["nodes_file"])
    links = gmc.load_vless_links()
    assigned = {node_key(l) for l in links.values()}
    print(f"пул: {len(pool)} нод в {DEFAULT_OPTS['nodes_file']}, назначено аккаунтам: {len(links)}, "
          f"свободных: {len([n for n in pool if n.key not in assigned])}")
    print(f"плохих нод (TTL): {len(st['bad'])}")
    for k, v in st["bad"].items():
        print(f"  {v.get('name')}  ({time.strftime('%d.%m %H:%M', time.localtime(v['ts']))}, "
              f"{v.get('account')}): {v.get('reason')}")
    print(f"история смен (последние 15 из {len(st['history'])}):")
    for h in st["history"][-15:]:
        print(f"  {time.strftime('%d.%m %H:%M', time.localtime(h['ts']))} {h['account']}: {h['summary']}")
