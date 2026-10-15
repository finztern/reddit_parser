#!/usr/bin/env python3
"""
scripts/add_guest_slots.py — добавляет гостевые слоты и сразу связывает их
со СВОБОДНЫМИ РАБОЧИМИ нодами из пула (all_nodes.txt).

Зачем скрипт, а не ручная правка: слот = порт + нода + отпечаток + гео, и
гео обязано соответствовать стране ноды (локаль/таймзона браузера при
харвесте, Accept-Language скрапера). Какие ноды свободны и живы, известно
только на сервере (all_nodes.txt, state/, mihomo), поэтому подбор делается
там.

Что делает (по умолчанию — ТОЛЬКО печатает план; запись — с --apply):

  1. Находит свободные ноды пула: не назначены ни одному аккаунту в
     vless_accounts.env, не числятся в реестре идентичностей демона ротации
     (state/identities.json) и не помечены плохими.
  2. Проверяет их живость через API mihomo (delay, как scripts/check_nodes.py);
     --no-probe — не проверять.
  3. Выбирает ноды с разбросом по странам; страну знает COUNTRY_GEO
     (rotation.py), ноды неизвестных стран берутся в последнюю очередь.
     --spare N (по умолчанию 8) нод остаются нетронутыми про запас для
     ротации — иначе демону будет некуда менять забаненные идентичности.
  4. Сначала закрывает ГОСТЕВЫЕ слоты, у которых нет ноды в
     vless_accounts.env (например account_25), потом создаёт --count новых
     слотов (account_{max+1}...) со свободными портами и уникальными
     отпечатками (impersonate берётся из установленного curl_cffi).
  5. Пишет (с бэкапами *.bak-addslots-<время>):
       vless_accounts.env                  account_N = vless://...
       accounts.yaml                       enabled: true, порт, locale, отпечаток
       scripts/refresh_guest_cookies.yaml  guest_accounts[N] (+geo под ноду)
       config.yaml                         rotation.slots += N
  6. Включает (enabled: true) все гостевые слоты, у которых есть нода
     (--no-enable-existing — не трогать уже существующие).
  7. --disable-login-without-node: выключает (enabled: false) ЛОГИН-аккаунты,
     у которых нет ноды в vless_accounts.env, — на чистом сервере без
     логин-аккаунтов их воркеры только копили бы сетевые ошибки.

Логин-аккаунты (account_1..10) не затрагиваются (кроме --disable-login-without-node).

После --apply нужно: сгенерировать mihomo_config.yaml, перезапустить mihomo,
сходить за cookies, перезапустить scraper — см. вывод скрипта.

Запуск (из корня проекта, тем же python, где стоит curl_cffi):

    venv/bin/python3 scripts/add_guest_slots.py --count 30            # план
    venv/bin/python3 scripts/add_guest_slots.py --count 30 --apply    # запись
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import socket
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import generate_mihomo_config as gmc  # noqa: E402
import rotate_fingerprints as rf  # noqa: E402
import rotation as rot  # noqa: E402

ACCOUNTS_PATH = ROOT / "accounts.yaml"
GUEST_PATH = SCRIPTS / "refresh_guest_cookies.yaml"
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = gmc.VLESS_ENV_PATH
IDENT_PATH = ROOT / "state" / "identities.json"

DEFAULT_PROBE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
REDDIT_PROBE_URL = "https://www.reddit.com/"
FIRST_NEW_PORT = 7916


# ---------------------------------------------------------------- #
#  Чтение состояния
# ---------------------------------------------------------------- #

def acc_num(name: str) -> int:
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def country_of(node_name: str) -> str:
    m = re.match(r"([a-z]{2})\d", node_name.strip().lower())
    return m.group(1) if m else "??"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_rotation_cfg() -> dict:
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    return {**rot.DEFAULT_OPTS, **(data.get("rotation") or {})}


@dataclass
class Free:
    node: "rot.Node"
    mihomo_name: str      # имя прокси в mihomo (как в сгенерированном конфиге)
    country: str
    delay: int | None = None


def find_free_nodes(pool_path: Path, assigned: dict[str, str], ident: dict) -> list[Free]:
    pool = rot.load_pool(pool_path)
    assigned_keys = {rot.node_key(l) for l in assigned.values()}
    reserved_names = set()
    for acc, link in assigned.items():
        try:
            reserved_names.add(gmc.parse_vless(link, acc)["name"])
        except ValueError:
            reserved_names.add(acc)
    names = rot.pool_names(pool, reserved_names, assigned_keys)

    known = {i.get("node_key") for i in (ident.get("identities") or {}).values()}
    now = time.time()
    bad = {k for k, b in (ident.get("bad_nodes") or {}).items()
           if now - b.get("ts", 0) < b.get("ttl_hours", 24) * 3600}
    bad |= {k for k, b in rot.load_state().get("bad", {}).items()}

    out = []
    for n in pool:
        if n.key in assigned_keys or n.key in known or n.key in bad:
            continue
        name = names.get(n.key)
        if name:
            out.append(Free(n, name, country_of(name)))
    return out


# ---------------------------------------------------------------- #
#  Проверка живости через mihomo
# ---------------------------------------------------------------- #

def probe_nodes(free: list[Free], api: str, secret: str, url: str, timeout_ms: int, workers: int,
                expected: str | None = None):
    """-> (живые, мёртвые, отсутствующие в mihomo). Падает на недоступном API.
    expected — ожидаемый HTTP-статус (параметр `expected` delay-API mihomo Meta): нода
    считается живой, только если URL вернул именно его (для Reddit — "200")."""
    m = rot.Mihomo(api, secret)
    q_url = urllib.parse.quote(url, safe="")

    def one(f: Free):
        path = f"/proxies/{urllib.parse.quote(f.mihomo_name, safe='')}/delay?url={q_url}&timeout={timeout_ms}"
        if expected:
            path += f"&expected={expected}"
        try:
            r = m._req("GET", path, timeout=timeout_ms / 1000 + 5) or {}
            d = r.get("delay")
            return f, ("ok" if d and d > 0 else "dead"), d
        except rot.RotationError as e:
            if e.fatal:
                raise
            return f, ("missing" if "HTTP 404" in str(e) else "dead"), None

    alive, dead, missing = [], [], []
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for f, status, d in ex.map(one, free):
            f.delay = d
            {"ok": alive, "dead": dead, "missing": missing}[status].append(f)
    return alive, dead, missing


# ---------------------------------------------------------------- #
#  Выбор нод и сборка плана
# ---------------------------------------------------------------- #

def pick_diverse(cands: list[Free], n: int, rng: random.Random) -> list[Free]:
    """Round-robin по странам; ноды известных стран — раньше неизвестных."""
    known = [c for c in cands if c.country in rot.COUNTRY_GEO]
    unknown = [c for c in cands if c.country not in rot.COUNTRY_GEO]

    def rr(items: list[Free], limit: int) -> list[Free]:
        groups: dict[str, list[Free]] = {}
        for it in items:
            groups.setdefault(it.country, []).append(it)
        for g in groups.values():
            rng.shuffle(g)
        order = sorted(groups, key=lambda k: (-len(groups[k]), rng.random()))
        res = []
        while len(res) < limit and any(groups.values()):
            for k in order:
                if groups[k] and len(res) < limit:
                    res.append(groups[k].pop())
        return res

    chosen = rr(known, n)
    if len(chosen) < n:
        chosen += rr(unknown, n - len(chosen))
    return chosen


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def allocate_ports(used: set[int], n: int, check=port_free) -> list[int]:
    out, p = [], max(max(used, default=FIRST_NEW_PORT - 1) + 1, FIRST_NEW_PORT)
    while len(out) < n:
        if p not in used and check(p):
            out.append(p)
        p += 1
    return out


def unique_link(uri: str, taken_names: set[str]) -> tuple[str, str]:
    """Имя прокси в mihomo берётся из #фрагмента ссылки и обязано быть
    уникальным — при коллизии дописываем ~N. -> (uri, имя)."""
    base = urllib.parse.unquote(urllib.parse.urlsplit(uri).fragment).strip() or uri[-12:]
    name, i = base, 2
    while name in taken_names:
        name = f"{base}~{i}"
        i += 1
    if name != base:
        uri = uri.split("#", 1)[0] + "#" + urllib.parse.quote(name, safe="~._-")
    taken_names.add(name)
    return uri, name


@dataclass
class SlotPlan:
    slot: str
    node: Free
    uri: str
    proxy_name: str
    geo: dict
    is_new: bool
    port: int | None = None
    profile: object | None = None   # rf.Profile для новых слотов


def pick_profiles(n: int, used_imps: set[str], rng: random.Random, fp_args) -> tuple[list, bool]:
    """n профилей с уникальными impersonate. Второе значение — пришлось ли
    повторять таргеты (когда установленный curl_cffi знает меньше таргетов)."""
    pool = [t for t in rf.build_pool(fp_args) if not (t.family == "safari" and t.major > 30)]
    if not pool:
        raise SystemExit("ОШИБКА: curl_cffi не дал ни одного подходящего impersonate-таргета.")
    free = [t for t in pool if t.name not in used_imps]
    rng.shuffle(free)
    reused = len(free) < n
    chosen = free[:n]
    while len(chosen) < n:
        chosen.append(rng.choice(pool))
    return [rf.build_profile(t, rng) for t in chosen], reused


# ---------------------------------------------------------------- #
#  Правка файлов (текстовая — комментарии сохраняются)
# ---------------------------------------------------------------- #

def _yq(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def account_block_text(plan: SlotPlan) -> str:
    p = plan.profile
    return (
        f"  - name: {plan.slot}\n"
        f"    enabled: true\n"
        f"    cookie_file: cookies/{plan.slot}.json\n"
        f"    proxy_port: {plan.port}\n"
        f"    locale: {plan.geo['locale']}\n"
        f"    impersonate: {p.impersonate}\n"
        f"    user_agent: {_yq(p.user_agent)}\n"
    )


def patch_account_fields(text: str, name: str, enabled: bool | None = None,
                         locale: str | None = None) -> str:
    """Меняет enabled/locale в блоке account `name` (вставляет locale после
    proxy_port, если строки не было). Отступ берётся из заменяемой строки,
    остальное не трогается."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"^\s*-\s*name:\s*{re.escape(name)}\s*$", l)), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*-\s*name:", lines[i])), len(lines))
    have_locale = False
    for i in range(start + 1, end):
        m = re.match(r"^(\s+)enabled:\s*", lines[i])
        if enabled is not None and m:
            lines[i] = f"{m.group(1)}enabled: {'true' if enabled else 'false'}\n"
        m = re.match(r"^(\s+)locale:\s*", lines[i])
        if locale is not None and m:
            lines[i] = f"{m.group(1)}locale: {locale}\n"
            have_locale = True
    if locale is not None and not have_locale:
        for i in range(start + 1, end):
            m = re.match(r"^(\s+)proxy_port:\s*", lines[i])
            if m:
                lines.insert(i + 1, f"{m.group(1)}locale: {locale}\n")
                break
    return "".join(lines)


def patch_account_geo_line(head: str, slot: str, geo: dict, label: str) -> str:
    """account_geo: `account_25: {locale: .., timezone_id: ..}  # sk1` в шапке
    guest yaml — единственное место вне guest_accounts, где живёт гео слота."""
    pat = re.compile(rf"(?m)^(\s*){re.escape(slot)}:\s*\{{[^}}]*\}}.*$")
    new = f'\\1{slot}: {{locale: "{geo["locale"]}", timezone_id: "{geo["timezone_id"]}"}}   # {label}'
    return pat.sub(new, head, count=1)


def patch_guest_yaml(text: str, plans: list[SlotPlan]) -> str:
    m = re.search(r"(?m)^guest_accounts:[ \t]*\n", text)
    if not m:
        raise SystemExit("ОШИБКА: в refresh_guest_cookies.yaml нет строки 'guest_accounts:'")
    if re.search(r"(?m)^[A-Za-z_]", text[m.end():]):
        raise SystemExit("ОШИБКА: после guest_accounts: в refresh_guest_cookies.yaml есть другие секции — "
                         "скрипт рассчитан на то, что она последняя (как и rotation.py). Добавь слоты вручную.")
    head = text[:m.start()]
    guest = (yaml.safe_load(text) or {}).get("guest_accounts") or {}
    for pl in plans:
        entry = dict(guest.get(pl.slot) or {})
        if pl.profile is not None:
            entry = rot.profile_entry(pl.profile)
        entry["geo"] = pl.geo
        guest[pl.slot] = entry
        head = patch_account_geo_line(head, pl.slot, pl.geo, pl.proxy_name)
    ordered = {k: guest[k] for k in sorted(guest, key=acc_num)}
    tail = yaml.safe_dump({"guest_accounts": ordered}, allow_unicode=True, sort_keys=False, width=200)
    return head + tail


def patch_config_slots(text: str, new_slots: list[str]) -> tuple[str, list[str]]:
    """Дописывает слоты в rotation.slots (блочный список). -> (текст, не вписанные)."""
    lines = text.splitlines(keepends=True)
    r = next((i for i, l in enumerate(lines) if re.match(r"^rotation:\s*(#.*)?$", l)), None)
    if r is None:
        return text, new_slots
    s = next((i for i in range(r + 1, len(lines)) if re.match(r"^  slots:\s*(#.*)?$", lines[i])), None)
    if s is None:
        return text, new_slots
    last, existing = s, set()
    for i in range(s + 1, len(lines)):
        m = re.match(r"^    -\s*(account_\d+)\s*(#.*)?$", lines[i])
        if m:
            existing.add(m.group(1))
            last = i
        elif lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            continue
        else:
            break
    add = [n for n in new_slots if n not in existing]
    for off, n in enumerate(add):
        lines.insert(last + 1 + off, f"    - {n}\n")
    return "".join(lines), []


def write_inplace(path: Path, text: str) -> None:
    """accounts.yaml и config.yaml примонтированы в контейнер как ОДИН файл —
    пишем в тот же inode, а не заменой файла."""
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
        f.flush()


# ---------------------------------------------------------------- #
#  main
# ---------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=30, help="сколько НОВЫХ слотов создать (по умолчанию 30)")
    ap.add_argument("--spare", type=int, default=8, help="сколько живых свободных нод оставить для ротации")
    ap.add_argument("--apply", action="store_true", help="записать изменения (без флага — только план)")
    ap.add_argument("--no-probe", action="store_true", help="не проверять живость нод через mihomo")
    ap.add_argument("--no-enable-existing", action="store_true",
                    help="не включать уже существующие гостевые слоты, у которых есть нода")
    ap.add_argument("--disable-login-without-node", action="store_true",
                    help="выключить логин-аккаунты, у которых нет ноды в vless_accounts.env "
                         "(чистый сервер без логин-аккаунтов)")
    ap.add_argument("--nodes-file", default=None, help="пул нод (по умолчанию rotation.nodes_file из config.yaml)")
    ap.add_argument("--probe-url", default=DEFAULT_PROBE_URL)
    ap.add_argument("--require-reddit", action="store_true",
                    help="брать только ноды, через которые Reddit отдаёт 200 (иначе забаненные "
                         "Reddit'ом, но живые ноды проходят проверку)")
    ap.add_argument("--probe-timeout-ms", type=int, default=8000)
    ap.add_argument("--probe-workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    if args.count < 0:
        sys.exit("ОШИБКА: --count должен быть >= 0")
    rng = random.Random(args.seed)

    accounts_text = ACCOUNTS_PATH.read_text(encoding="utf-8")
    guest_text = GUEST_PATH.read_text(encoding="utf-8")
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    accounts = (yaml.safe_load(accounts_text) or {}).get("accounts") or []
    guest = (yaml.safe_load(guest_text) or {}).get("guest_accounts") or {}
    by_name = {a["name"]: a for a in accounts}
    links = gmc.load_vless_links()
    ident = read_json(IDENT_PATH)
    ropts = load_rotation_cfg()

    # --- слоты, которым нужна нода ---
    missing_nodes = sorted((n for n in guest if n in by_name and n not in links), key=acc_num)
    want_total = len(missing_nodes) + args.count
    if want_total == 0:
        print("Новых слотов не нужно (--count 0, у всех гостевых слотов есть нода).")

    # --- свободные ноды ---
    nf = Path(args.nodes_file or ropts["nodes_file"])
    nf = nf if nf.is_absolute() else ROOT / nf
    if want_total and not nf.exists():
        sys.exit(f"ОШИБКА: нет пула нод {nf} (all_nodes.txt) — свободные ноды взять неоткуда.")
    free = find_free_nodes(nf, links, ident) if want_total else []
    if want_total:
        print(f"Пул {nf.name}: свободных (не назначены, нет в реестре ротации, не плохие): {len(free)}")

    if not want_total:
        alive = []
    elif args.no_probe:
        alive = free
    else:
        try:
            alive, dead, absent = probe_nodes(free, ropts["mihomo_api"], ropts.get("mihomo_secret", ""),
                                              (REDDIT_PROBE_URL if args.require_reddit else args.probe_url),
                                              args.probe_timeout_ms, args.probe_workers,
                                              "200" if args.require_reddit else None)
        except rot.RotationError as e:
            sys.exit(f"ОШИБКА: {e}\nЗапусти mihomo (docker compose up -d mihomo) или используй --no-probe.")
        print(f"Проверка через mihomo: живых {len(alive)}, не отвечают {len(dead)}, "
              f"нет в mihomo {len(absent)}")
        if absent and not alive:
            sys.exit("ОШИБКА: пул нод не загружен в запущенный mihomo. Выполни:\n"
                     "  venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo")
        if absent:
            print(f"  (ноды, которых нет в mihomo, пропущены: {len(absent)} — они появились в all_nodes.txt "
                  "после последней генерации конфига)")

    available = max(0, len(alive) - max(0, args.spare))
    take = min(want_total, available)
    if take < want_total:
        print(f"ВНИМАНИЕ: нужно {want_total} нод, доступно {available} "
              f"(живых {len(alive)} минус запас {args.spare}) — создаю {take}.")
    if want_total and take == 0:
        sys.exit("Свободных нод для новых слотов нет (или всё ушло в запас) — ничего не делаю.")

    chosen = pick_diverse(alive, take, rng)
    unknown_cc = sorted({c.country for c in chosen if c.country not in rot.COUNTRY_GEO})
    if unknown_cc:
        print(f"ВНИМАНИЕ: страны нод {', '.join(unknown_cc)} нет в COUNTRY_GEO (rotation.py) — "
              "для них гео будет en-US/New_York; допиши страну в COUNTRY_GEO для верного гео.")

    # --- слоты: сначала существующие без ноды, потом новые ---
    n_missing = min(len(missing_nodes), len(chosen))
    n_new = len(chosen) - n_missing
    new_start = max((acc_num(a["name"]) for a in accounts), default=0) + 1
    new_slots = [f"account_{new_start + i}" for i in range(n_new)]

    used_ports = {int(a["proxy_port"]) for a in accounts if a.get("proxy_port")}
    ports = allocate_ports(used_ports, n_new)

    used_imps = {a.get("impersonate") for a in accounts} | {
        (i.get("fp") or {}).get("impersonate") for i in (ident.get("identities") or {}).values()}
    fp_args = SimpleNamespace(allow_old=False, no_edge=True, no_mobile=False)
    profiles, reused = pick_profiles(n_new, used_imps, rng, fp_args) if n_new else ([], False)
    if reused:
        print("ВНИМАНИЕ: в установленном curl_cffi меньше свободных impersonate-таргетов, чем новых "
              "слотов — часть таргетов повторится (UA/платформа/устройство при этом разные).")

    taken_names = set()
    for acc, link in links.items():
        try:
            taken_names.add(gmc.parse_vless(link, acc)["name"])
        except ValueError:
            taken_names.add(acc)

    plans: list[SlotPlan] = []
    for i, f in enumerate(chosen):
        uri, pname = unique_link(f.node.uri, taken_names)
        geo = rot.geo_for(f.mihomo_name)
        if i < n_missing:
            plans.append(SlotPlan(missing_nodes[i], f, uri, pname, geo, is_new=False))
        else:
            j = i - n_missing
            plans.append(SlotPlan(new_slots[j], f, uri, pname, geo, is_new=True,
                                  port=ports[j], profile=profiles[j]))

    # существующие гостевые слоты с нодой — включить
    to_enable = []
    if not args.no_enable_existing:
        planned = {p.slot for p in plans}
        for name in sorted(guest, key=acc_num):
            a = by_name.get(name)
            if a and name in links and name not in planned and not a.get("enabled", True):
                to_enable.append(name)

    login_off = []
    if args.disable_login_without_node:
        login_off = [a["name"] for a in accounts
                     if a["name"] not in guest and a.get("enabled") and a["name"] not in links]

    if not plans and not to_enable and not login_off:
        print("Изменений нет — выхожу.")
        return 0

    # --- план ---
    print(f"\n{'слот':<11} {'порт':<5} {'нода (mihomo)':<24} {'гео':<7} {'impersonate':<18} {'engine':<9} примечание")
    for p in plans:
        port = p.port or by_name[p.slot].get("proxy_port")
        imp = p.profile.impersonate if p.profile else by_name[p.slot].get("impersonate")
        eng = (p.profile.engine if p.profile else (guest.get(p.slot) or {}).get("engine", ""))
        print(f"{p.slot:<11} {port!s:<5} {p.proxy_name[:24]:<24} {p.geo['locale']:<7} {imp!s:<18} {eng!s:<9} "
              f"{'НОВЫЙ слот' if p.is_new else 'нода для существующего слота'}")
    if to_enable:
        print(f"\nБудут включены (enabled: true), нода у них уже есть: {', '.join(to_enable)}")
    if login_off:
        print(f"\nБудут ВЫКЛЮЧЕНЫ логин-аккаунты без ноды (enabled: false): {', '.join(login_off)}")
    left = len(alive) - len(chosen)
    print(f"\nЖивых свободных нод останется на ротацию: {left}")
    if left < 5:
        print("  ВНИМАНИЕ: запас на ротацию мал — демону будет нечего выдавать забаненным слотам.")

    if not args.apply:
        print("\n[план] ничего не записано. Для записи добавь --apply")
        return 0

    # --- запись ---
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (ENV_PATH, ACCOUNTS_PATH, GUEST_PATH, CONFIG_PATH):
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-addslots-{stamp}"))

    # vless_accounts.env
    if plans:
        env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        env_text = env_text.rstrip("\n") + f"\n\n# --- добавлено scripts/add_guest_slots.py {stamp} ---\n"
        env_text += "".join(f"{p.slot} = {p.uri}\n" for p in plans)
        ENV_PATH.write_text(env_text, encoding="utf-8")

    # accounts.yaml
    new_accounts = accounts_text
    for p in plans:
        if not p.is_new:
            new_accounts = patch_account_fields(new_accounts, p.slot, enabled=True, locale=p.geo["locale"])
    for name in to_enable:
        new_accounts = patch_account_fields(new_accounts, name, enabled=True)
    for name in login_off:
        new_accounts = patch_account_fields(new_accounts, name, enabled=False)
    added = [p for p in plans if p.is_new]
    if added:
        new_accounts = (new_accounts.rstrip("\n") + f"\n\n  # --- добавлено scripts/add_guest_slots.py {stamp} ---\n\n"
                        + "\n".join(account_block_text(p) for p in added))
    write_inplace(ACCOUNTS_PATH, new_accounts)

    # refresh_guest_cookies.yaml
    if plans:
        GUEST_PATH.write_text(patch_guest_yaml(guest_text, plans), encoding="utf-8")

    # config.yaml: rotation.slots
    slot_names = [p.slot for p in plans]
    new_config, not_written = patch_config_slots(config_text, slot_names)
    if new_config != config_text:
        write_inplace(CONFIG_PATH, new_config)
    if not_written:
        print("\nВНИМАНИЕ: не смог вписать слоты в rotation.slots (config.yaml нестандартного вида) — "
              f"добавь вручную: {', '.join(not_written)}")

    # самопроверка: файлы читаются и согласованы
    chk = (yaml.safe_load(ACCOUNTS_PATH.read_text(encoding="utf-8")) or {}).get("accounts") or []
    ports_all = [a["proxy_port"] for a in chk if a.get("proxy_port")]
    if len(ports_all) != len(set(ports_all)):
        print("ОШИБКА: после записи в accounts.yaml есть дубли proxy_port — восстанови из *.bak-addslots-*")
        return 1
    yaml.safe_load(GUEST_PATH.read_text(encoding="utf-8"))

    print(f"\nOK: записано (бэкапы *.bak-addslots-{stamp}): vless_accounts.env, accounts.yaml, "
          "scripts/refresh_guest_cookies.yaml, config.yaml")
    print("\nДальше (из корня проекта):")
    print("  1) venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo")
    print("  2) venv/bin/python3 scripts/refresh_guest_cookies.py      # cookies для новых слотов, параллельно")
    print("  3) sudo systemctl restart rotation-daemon                 # подхватит слоты и реестр нод")
    print("  4) docker compose up -d --build scraper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
