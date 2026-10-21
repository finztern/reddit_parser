#!/usr/bin/env python3
"""
scripts/attach_proxies.py — аккаунты + автоподбор прокси/нод из файла all_proxies.txt.

Режим --mode login (по умолчанию): ЛОГИН-слоты (login: true, enabled: false до входа через noVNC, запись в
refresh_cookies.yaml:login_accounts/account_geo). --mode guest: гостевые слоты (guest_accounts, rotation.slots).
Запись «всё или ничего»: все файлы формируются и проверяются в памяти, на диск пишется только после этого.

Идея: ты кладёшь прокси (по одному в строке: user:pass@host:port, scheme://..., host:port:user:pass)
в all_proxies.txt, а скрипт сам решает, какой аккаунт на каком прокси сидит. Жёсткая привязка руками
не нужна. VLESS-ноды из all_nodes.txt участвуют в подборе наравне с прокси (--sources proxy — только
прокси).

  add      создаёт новые аккаунты (и достаёт узел слотам режима, у которых его нет), каждому
           цепляет прокси. Кандидаты сначала проверяются на доступность Reddit (robots.txt = 200) и сортируются
           по скорости: самые быстрые идут первыми аккаунтами, самые медленные остаются в конце очереди.
           Для каждого аккаунта создаётся профиль устройства (state/profiles), гео — по выходному IP
           прокси (locale/timezone совпадают со страной).
  replace  у аккаунтов, где при входе через noVNC вылезла капча или Reddit недоступен, меняет ПРОФИЛЬ
           и ПРОКСИ: берётся следующий по скорости свободный прокси, генерируется другое устройство
           (другой отпечаток), старый прокси уходит в чёрный список (по умолчанию на 72 ч).
             --accounts 11 12 account_13   явный список
             --from-hub                    аккаунты, у которых login_hub показывает капчу
             --auto                        проверить доступность Reddit через текущие прокси
             --force                       (login) заменять и аккаунты с сохранённой сессией — сессия слетит
  reprofile пересоздаёт профиль устройства БЕЗ смены прокси (по умолчанию — только мобильные -> десктоп).
  rank     только показать очередь «кто быстрее» (ничего не пишет).

Профили по умолчанию ТОЛЬКО десктопные (--allow-mobile разрешает Android/iOS).

Как всегда в репозитории: без --apply печатается ТОЛЬКО план. Перед записью делаются бэкапы
*.bak-attach-<время>. Остановите rotation-daemon на время запуска.

    venv/bin/python3 scripts/attach_proxies.py rank
    venv/bin/python3 scripts/attach_proxies.py add --count 10            # план
    venv/bin/python3 scripts/attach_proxies.py add --count 10 --apply
    # ... зашёл по noVNC (login_hub.py --accounts account_N), увидел капчу/недоступность:
    venv/bin/python3 scripts/attach_proxies.py replace --accounts 12 --apply
    venv/bin/python3 scripts/attach_proxies.py replace --from-hub --apply

Учётные данные прокси нигде не печатаются.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import add_guest_slots as ags  # noqa: E402
import generate_mihomo_config as gmc  # noqa: E402
import login_fleet as lf  # noqa: E402
import rotation as rot  # noqa: E402
from scraper import geoip, profile_gen as pg  # noqa: E402
from scraper.identity import write_json_atomic  # noqa: E402

ACCOUNTS_PATH = ags.ACCOUNTS_PATH
GUEST_PATH = ags.GUEST_PATH
CONFIG_PATH = ags.CONFIG_PATH
ENV_PATH = gmc.VLESS_ENV_PATH
STATE_DIR = ROOT / "state"
IDENT_PATH = STATE_DIR / "identities.json"
PA_STATE_PATH = STATE_DIR / "proxy_attach.json"          # чёрный список прокси и история замен
HUB_STATUS_PATH = STATE_DIR / "login_hub" / "status.json"
GEO_CACHE_PATH = STATE_DIR / "proxy_geo.json"
LOGIN_PATH = SCRIPTS / "refresh_cookies.yaml"           # login_accounts + account_geo
STORAGE_DIR = ROOT / "storage_state"                    # сохранённые сессии входа

DEFAULT_PROXIES_FILE = "all_proxies.txt"
# robots.txt: Reddit отдаёт его анониму без cookies (главная и .json без cookies/браузерного отпечатка дают 403
# даже через рабочий прокси). Проверка = прокси жив, до Reddit достаёт, IP не забанен целиком; пустит ли Reddit
# в браузер, выяснится при входе через noVNC (потом replace). Другой URL — --probe-url.
REDDIT_URL = "https://www.reddit.com/robots.txt"
DEFAULT_BAD_TTL_HOURS = 72
# Короткая страница-заглушка с этими словами = капча/блокировка (настоящая главная Reddit — сотни КБ).
CHALLENGE_MARKERS = ("captcha", "verify you are human", "are you a human", "prove you're not a robot",
                     "blocked by network security", "whoa there, pardner")
CHALLENGE_MAX_BYTES = 40_000


# ---------------------------------------------------------------- #
#  Сеть: замер скорости и гео через сам прокси
# ---------------------------------------------------------------- #

def judge_response(status: int, body_len: int, text: str, ms: int) -> tuple[int | None, str | None]:
    """-> (задержка_мс, None) для годного ответа или (None, причина). Годный = 200 и не страница-заглушка."""
    if status != 200:
        return None, f"HTTP {status}"
    if body_len < CHALLENGE_MAX_BYTES and any(m in text.lower() for m in CHALLENGE_MARKERS):
        return None, "капча/блокировка"
    return ms, None


def _curl_requests():
    try:
        from curl_cffi import requests as cr
    except ImportError:
        sys.exit("ОШИБКА: нужен curl_cffi (venv/bin/pip install -r requirements.txt)")
    return cr


def probe_proxy(uri: str, url: str = REDDIT_URL, timeout: float = 8.0) -> tuple[int | None, str | None]:
    """Запрос на Reddit ЧЕРЕЗ прокси напрямую (mihomo не нужен). -> (мс, None) | (None, причина).
    Текст исключения не возвращаем: в нём может оказаться адрес прокси с логином."""
    cr = _curl_requests()
    proxy = rot.node_key(uri)
    t0 = time.perf_counter()
    try:
        r = cr.get(url, proxies={"http": proxy, "https": proxy}, timeout=timeout, impersonate="chrome",
                   allow_redirects=True)
    except Exception as e:  # noqa: BLE001 — любая сетевая ошибка = прокси негоден
        return None, type(e).__name__
    ms = int((time.perf_counter() - t0) * 1000)
    return judge_response(r.status_code, len(r.content or b""), (r.text or "")[:6000], ms)


def curl_fetch(url: str, proxy_url: str | None, timeout: float = 15.0) -> str:
    """fetch для geoip.resolve_geo: через curl_cffi, поэтому работает и с socks5 (urllib — нет)."""
    cr = _curl_requests()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    r = cr.get(url, proxies=proxies, timeout=timeout, headers={"User-Agent": "curl/8.5.0"})
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- #
#  Состояние: файлы, чёрный список
# ---------------------------------------------------------------- #

def load_rotation_cfg() -> dict:
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    return {**rot.DEFAULT_OPTS, **(data.get("rotation") or {})}


def read_links() -> dict[str, str]:
    """account_N -> нормализованная ссылка из vless_accounts.env (как generate_mihomo_config)."""
    links: dict[str, str] = {}
    if not ENV_PATH.exists():
        return links
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        m = gmc.ACCOUNT_LINE_RE.match(raw.strip()) if not raw.strip().startswith("#") else None
        if m and (norm := gmc.normalize_link(m.group(2))):
            links[m.group(1)] = norm
    return links


def load_pa_state() -> dict:
    st = ags.read_json(PA_STATE_PATH)
    st.setdefault("bad", {})
    st.setdefault("history", [])
    return st


def active_bad_keys(st: dict, now: float | None = None) -> set[str]:
    now = time.time() if now is None else now
    return {k for k, b in st.get("bad", {}).items() if now - b.get("ts", 0) < b.get("ttl_hours", 24) * 3600}


def resolve_proxies_file(arg: str | None, ropts: dict) -> Path:
    """--proxies-file, иначе all_proxies.txt, иначе файл из rotation.proxies_file (all_proxys.txt)."""
    if arg:
        p = Path(arg)
        return p if p.is_absolute() else ROOT / p
    main = ROOT / DEFAULT_PROXIES_FILE
    if main.exists():
        return main
    legacy = ROOT / str(ropts.get("proxies_file") or "")
    return legacy if ropts.get("proxies_file") and legacy.exists() else main


# ---------------------------------------------------------------- #
#  Кандидаты: свободные узлы -> проверка -> очередь по скорости
# ---------------------------------------------------------------- #

def collect_free(proxies_file: Path, sources: list[str], links: dict[str, str], ident: dict,
                 pa_state: dict, ropts: dict) -> list["ags.Free"]:
    nf = Path(ropts["nodes_file"])
    nf = nf if nf.is_absolute() else ROOT / nf
    if not rot.pools_exist(nf, str(proxies_file)):
        sys.exit(f"ОШИБКА: нет ни {proxies_file.name}, ни {nf.name} — взять узлы неоткуда.")
    free = ags.find_free_nodes(nf, links, ident, sources, str(proxies_file))
    bad = active_bad_keys(pa_state)
    return [f for f in free if f.node.key not in bad]


def rank_nodes(nodes: list["ags.Free"]) -> list["ags.Free"]:
    """Быстрые первыми; неизмеренные (VLESS без mihomo) — в самом конце."""
    return sorted(nodes, key=lambda f: (f.delay is None, f.delay if f.delay is not None else 0, f.mihomo_name))


def measure(free: list["ags.Free"], ropts: dict, url: str, timeout_ms: int, workers: int,
            probe: bool = True) -> tuple[list["ags.Free"], dict]:
    """Проверяет узлы на Reddit и ставит f.delay. Прокси — напрямую, VLESS — через delay-API mihomo.
    -> (живые в порядке «быстрые первыми», статистика)."""
    stats = {"total": len(free), "dead": collections.Counter(), "unmeasured": 0}
    if not probe:
        stats["unmeasured"] = len(free)
        return list(free), stats
    prox = [f for f in free if gmc.link_kind(f.node.uri) == "proxy"]
    vless = [f for f in free if gmc.link_kind(f.node.uri) != "proxy"]
    alive: list[ags.Free] = []

    def one(f):
        return f, probe_proxy(f.node.uri, url, timeout_ms / 1000)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        for f, (ms, why) in ex.map(one, prox):
            if ms is None:
                stats["dead"][why or "?"] += 1
            else:
                f.delay = ms
                alive.append(f)
    if vless:
        try:
            ok, dead, absent = ags.probe_nodes(vless, ropts["mihomo_api"], ropts.get("mihomo_secret", ""),
                                               url, timeout_ms, workers, "200")
            alive += ok + absent            # absent: ещё не загружены в mihomo — без замера, в конец
            stats["dead"]["VLESS: Reddit не пускает"] += len(dead)
            stats["unmeasured"] += len(absent)
        except rot.RotationError as e:
            print(f"ВНИМАНИЕ: mihomo недоступен ({e}) — {len(vless)} VLESS-нод без замера скорости, "
                  "они в конце очереди.")
            alive += vless
            stats["unmeasured"] += len(vless)
    return rank_nodes(alive), stats


def print_stats(stats: dict, alive: list["ags.Free"]) -> None:
    dead = sum(stats["dead"].values())
    print(f"Проверено {stats['total']}: годных {len(alive)}, негодных {dead}"
          + (f" ({', '.join(f'{k}: {v}' for k, v in stats['dead'].most_common(4))})" if dead else "")
          + (f", без замера {stats['unmeasured']}" if stats["unmeasured"] else ""))
    if alive:
        head = ", ".join(f"{f.mihomo_name} {f.delay}мс" if f.delay is not None else f"{f.mihomo_name} ?"
                         for f in alive[:5])
        print(f"Самые быстрые: {head}")
        slow = [f for f in alive[-3:] if f.delay is not None]
        if len(alive) > 8 and slow:
            print(f"Самые медленные: {', '.join(f'{f.mihomo_name} {f.delay}мс' for f in slow)}")


# ---------------------------------------------------------------- #
#  Гео и профиль устройства
# ---------------------------------------------------------------- #

def _fill_geo_meta(geo: dict, source: str) -> dict:
    geo.update({"geo_source": source, "exit_ip": None, "checked_at": int(time.time())})
    return geo


def geo_for_node(f: "ags.Free", live: bool = True) -> tuple[dict, str | None]:
    """Гео слота для узла. Прокси: по выходному IP (через сам прокси); иначе/при сбое — по коду страны
    в имени узла; совсем неизвестно — en-US с пометкой geo_note. -> (geo, предупреждение|None)."""
    warn = None
    if live and gmc.link_kind(f.node.uri) == "proxy":
        for attempt in range(2):                      # сервис гео через прокси иногда отвечает не с первого раза
            try:
                return geoip.resolve_geo(rot.node_key(f.node.uri), fetch=curl_fetch), None
            except Exception as e:  # noqa: BLE001
                warn = f"{f.mihomo_name}: страна по выходному IP не определена ({type(e).__name__})"
    cc = rot.country_for(f.mihomo_name)
    if cc != "??":
        return _fill_geo_meta(geoip.geo_for_country(cc.upper()), "node-name"), warn
    geo = _fill_geo_meta(geoip.geo_for_country(None), "unknown")
    return geo, (warn or f"{f.mihomo_name}: страна неизвестна — гео en-US (проверьте профиль)")


def existing_fingerprints(state_dir: Path, skip: str | None = None) -> set[str]:
    d = pg.profiles_dir(state_dir)
    out = set()
    for fpath in (d.glob("account_*.json") if d.exists() else []):
        if fpath.stem == skip:
            continue
        p = pg.load_profile(fpath.stem, state_dir)
        if p:
            out.add(pg.fingerprint(p))
    return out


def fresh_profile(slot: str, geo: dict, presets: dict, taken: set[str], avoid_imps: set[str],
                  base_seed: int, no_mobile: bool = True) -> dict:
    """Новое устройство: отпечаток не совпадает ни с одним из taken и (пока есть варианты) impersonate
    не из avoid_imps; no_mobile — только десктоп (без Android/iOS). taken пополняется."""
    last = None
    for attempt in range(80):
        p = pg.generate_profile(slot, pg.derive_seed(base_seed, slot, attempt), geo=geo, presets=presets)
        last = p
        if no_mobile and p.get("mobile"):
            continue
        if pg.fingerprint(p) in taken:
            continue
        if p["impersonate"] in avoid_imps and attempt < 50:
            continue
        taken.add(pg.fingerprint(p))
        return p
    raise pg.ProfileError(f"{slot}: не удалось подобрать новое устройство (последний вариант: "
                          f"{last and last['impersonate']})")


def profile_with_geo(cur: dict, geo: dict) -> dict:
    """То же, что pg.update_geo, но без записи — чтобы проверить профиль ДО изменений файлов."""
    new = dict(cur)
    for k in pg.GEO_FIELDS:
        if k in geo:
            new[k] = geo[k]
    new["accept_language"] = pg.build_accept_language(new["locale"], new["impersonate"])
    return new


def guest_profile_view(p: dict) -> SimpleNamespace:
    """Поля, нужные ags.patch_guest_yaml / rot.profile_entry."""
    return SimpleNamespace(engine=p["engine"], channel=p.get("channel"), device=p.get("device"),
                           user_agent=p["user_agent"], impersonate=p["impersonate"])


# ---------------------------------------------------------------- #
#  Правка текстовых файлов (комментарии сохраняются)
# ---------------------------------------------------------------- #

def _fmt_value(key: str, value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return ags._yq(str(value)) if key == "user_agent" else str(value)


def patch_account_keys(text: str, name: str, values: dict) -> str:
    """Меняет/добавляет ключи в блоке account `name` accounts.yaml; новые ключи — после proxy_port."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^\s*-\s*name:\s*{re.escape(name)}\s*$", ln)), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*-\s*name:", lines[i])), len(lines))
    pending = dict(values)
    for i in range(start + 1, end):
        m = re.match(r"^(\s+)(\w+):", lines[i])
        if m and m.group(2) in pending:
            key = m.group(2)
            lines[i] = f"{m.group(1)}{key}: {_fmt_value(key, pending.pop(key))}\n"
    if pending:
        at = next((i for i in range(start + 1, end) if re.match(r"^\s+proxy_port:", lines[i])), start)
        indent = re.match(r"^(\s*)-", lines[start]).group(1) + "  "
        for off, (k, v) in enumerate(pending.items()):
            lines.insert(at + 1 + off, f"{indent}{k}: {_fmt_value(k, v)}\n")
    return "".join(lines)


def account_block(slot: str, port: int, profile: dict, locale: str, enabled: bool) -> str:
    return (f"  - name: {slot}\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            f"    cookie_file: cookies/{slot}.json\n"
            f"    proxy_port: {port}\n"
            f"    locale: {locale}\n"
            f"    impersonate: {profile['impersonate']}\n"
            f"    user_agent: {ags._yq(profile['user_agent'])}\n")


def set_env_links(text: str, assignments: dict[str, str], stamp: str) -> str:
    """account_N = ссылка: существующая строка заменяется, новые дописываются в конец."""
    appended = []
    for acc, uri in assignments.items():
        pat = re.compile(rf"(?m)^[ \t]*{re.escape(acc)}[ \t]*=.*$")
        if pat.search(text):
            text = pat.sub(lambda _m, a=acc, u=uri: f"{a} = {u}", text, count=1)
        else:
            appended.append(f"{acc} = {uri}\n")
    if appended:
        text = text.rstrip("\n") + (f"\n\n# --- добавлено scripts/attach_proxies.py {stamp} ---\n" if text.strip() else "")
        text += "".join(appended)
    return text


def parse_accounts(tokens: list[str], known: list[str]) -> list[str]:
    """account_11 | 11 | 11-15 | 11,12 -> имена (порядок сохраняется, дубли убираются)."""
    out: list[str] = []
    for tok in tokens:
        for part in (p.strip() for p in tok.split(",")):
            if not part:
                continue
            rng = re.fullmatch(r"(?:account_)?(\d+)-(?:account_)?(\d+)", part)
            if rng:
                names = [f"account_{n}" for n in range(int(rng.group(1)), int(rng.group(2)) + 1)]
            elif re.fullmatch(r"(?:account_)?\d+", part):
                names = [f"account_{int(part.removeprefix('account_'))}"]
            else:
                names = [part]
            for n in names:
                if n not in known:
                    sys.exit(f"ОШИБКА: {n} нет в accounts.yaml")
                if n not in out:
                    out.append(n)
    return out


def hub_captcha_accounts(path: Path | None = None) -> list[str]:
    """Аккаунты, у которых сессия login_hub сейчас ждёт человека из-за капчи."""
    data = ags.read_json(path or HUB_STATUS_PATH)
    return [s["account"] for s in data.get("sessions") or []
            if s.get("status") == "waiting_user" and "captcha" in str(s.get("message", "")).lower()]


# ---------------------------------------------------------------- #
#  План и запись
# ---------------------------------------------------------------- #

@dataclass
class Change:
    slot: str
    node: "ags.Free"
    uri: str                    # ссылка с уникальным именем (то, что пойдёт в vless_accounts.env)
    proxy_name: str
    geo: dict
    profile: dict               # профиль устройства (новый, либо текущий с обновлённым гео)
    profile_op: str             # "create" | "reset" | "geo"
    is_new: bool = False
    port: int | None = None
    old_key: str | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def taken_names_of(links: dict[str, str]) -> set[str]:
    names = set()
    for acc, link in links.items():
        try:
            names.add(gmc.parse_node(link, acc)["name"])
        except ValueError:
            names.add(acc)
    return names


def print_plan(changes: list[Change], by_name: dict, mode: str = "login") -> None:
    print(f"\nРежим: {mode}")
    print(f"{'слот':<11} {'порт':<5} {'узел':<26} {'мс':<6} {'гео':<7} {'impersonate':<18} примечание")
    for c in changes:
        port = c.port or by_name.get(c.slot, {}).get("proxy_port")
        ms = c.node.delay if c.node.delay is not None else "?"
        new_note = "НОВЫЙ логин-слот (enabled: false до входа)" if mode == "login" else "НОВЫЙ слот"
        note = {"create": new_note if c.is_new else "узел для существующего слота + новый профиль",
                "reset": f"ЗАМЕНА профиля и прокси ({c.reason})" if c.reason else "узел для слота + замена профиля",
                "geo": "узел для существующего слота (профиль прежний)"}[c.profile_op]
        print(f"{c.slot:<11} {port!s:<5} {c.proxy_name[:26]:<26} {ms!s:<6} {c.geo['locale']:<7} "
              f"{c.profile['impersonate']:<18} {note}")
        for w in c.warnings:
            print(f"    ! {w}")


def next_steps(changes: list[Change], replaced: bool, mode: str) -> None:
    names = " ".join(c.slot for c in changes)
    print("\nДальше (из корня проекта):")
    print("  1) venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo")
    print(f"  2) venv/bin/python3 scripts/login_hub.py --accounts {names}   # зайти по noVNC"
          + ("  (старые окна этих аккаунтов закрой)" if replaced else ""))
    if mode == "guest":
        print("     (если вход не нужен — cookies гостя: venv/bin/python3 scripts/refresh_guest_cookies.py "
              + " ".join(f"--account {c.slot}" for c in changes) + ")")
    print("  3) sudo systemctl restart rotation-daemon")
    print("  4) docker compose up -d --build scraper")
    print("  Капча / Reddit недоступен у какого-то аккаунта — "
          "venv/bin/python3 scripts/attach_proxies.py replace --accounts N --apply")


# ---------------------------------------------------------------- #
#  Состав парка и запись «всё или ничего»
# ---------------------------------------------------------------- #

@dataclass
class Fleet:
    """Файлы и состав режима (login | guest), прочитанные один раз за запуск."""
    mode: str
    accounts_text: str
    accounts: list
    by_name: dict
    members: set            # слоты этого режима
    mode_text: str          # refresh_cookies.yaml (login) | refresh_guest_cookies.yaml (guest)
    config_text: str

    @property
    def mode_path(self) -> Path:
        return LOGIN_PATH if self.mode == "login" else GUEST_PATH


def load_fleet(mode: str) -> Fleet:
    accounts_text = ACCOUNTS_PATH.read_text(encoding="utf-8")
    accounts = (yaml.safe_load(accounts_text) or {}).get("accounts") or []
    config_text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    path = LOGIN_PATH if mode == "login" else GUEST_PATH
    if not path.exists():
        sys.exit(f"ОШИБКА: нет {path.name} (режим --mode {mode}).")
    text = path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text) or {}
    members = (lf.login_account_names(cfg, accounts) if mode == "login"
               else set(cfg.get("guest_accounts") or {}))
    return Fleet(mode, accounts_text, accounts, {a["name"]: a for a in accounts}, members, text, config_text)


def has_session(slot: str) -> bool:
    """Есть сохранённая сессия входа (storage_state) — замена прокси/профиля её сбросит."""
    return (STORAGE_DIR / f"{slot}.json").exists()


def fleet_problems(accounts: list, links: dict, mode: str, mode_text: str) -> set[str]:
    cfg = yaml.safe_load(mode_text) or {}
    if mode == "login":
        return set(lf.validate_fleet(accounts, links, None, cfg))
    return set(lf.validate_fleet(accounts, links, cfg.get("guest_accounts") or {}))


@dataclass
class Rendered:
    env: str
    accounts: str
    mode_text: str
    config: str | None      # None — config.yaml не меняется


def render_changes(changes: list[Change], fleet: Fleet, links: dict[str, str], *, enabled: bool,
                   stamp: str) -> Rendered:
    """Все новые тексты и их проверка — БЕЗ записи на диск: при любой ошибке файлы остаются нетронутыми."""
    env_old = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    env = set_env_links(env_old, {c.slot: c.uri for c in changes}, stamp)

    accounts, appended = fleet.accounts_text, []
    for c in changes:
        if c.is_new:
            appended.append(
                lf.login_account_block(c.slot, c.port, c.geo["locale"], c.profile["impersonate"],
                                       c.profile["user_agent"]) if fleet.mode == "login"
                else account_block(c.slot, c.port, c.profile, c.geo["locale"], enabled))
            continue
        kv = {"locale": c.geo["locale"]}
        if fleet.mode == "guest" and c.profile_op != "reset":
            kv["enabled"] = enabled
        if c.profile_op != "geo":
            kv.update(impersonate=c.profile["impersonate"], user_agent=c.profile["user_agent"])
        accounts = patch_account_keys(accounts, c.slot, kv)
    if appended:
        what = "логин-слоты, enabled: false до входа" if fleet.mode == "login" else "гостевые слоты"
        accounts = (accounts.rstrip("\n") + f"\n\n  # --- добавлено scripts/attach_proxies.py {stamp} ({what}) ---\n\n"
                    + "\n".join(appended))

    config = None
    if fleet.mode == "login":
        geo_by = {c.slot: {"locale": c.geo["locale"], "timezone_id": c.geo["timezone_id"]} for c in changes}
        try:
            mode_text = lf.patch_login_yaml(fleet.mode_text, [c.slot for c in changes], geo_by,
                                            {c.slot: c.proxy_name for c in changes},
                                            overwrite_geo={c.slot for c in changes if not c.is_new})
        except ValueError as e:
            sys.exit(f"ОШИБКА: {e}")
    else:
        plans = [ags.SlotPlan(c.slot, c.node, c.uri, c.proxy_name,
                              {"locale": c.geo["locale"], "timezone_id": c.geo["timezone_id"]}, c.is_new, c.port,
                              guest_profile_view(c.profile) if c.profile_op != "geo" else None) for c in changes]
        mode_text = ags.patch_guest_yaml(fleet.mode_text, plans)
        new_slots = [c.slot for c in changes if c.is_new]
        if new_slots:
            new_config, not_written = ags.patch_config_slots(fleet.config_text, new_slots)
            config = new_config if new_config != fleet.config_text else None
            if not_written:
                print(f"ВНИМАНИЕ: не смог вписать в rotation.slots (config.yaml нестандартного вида): "
                      f"{', '.join(not_written)} — добавьте вручную.")

    links2 = {**links, **{c.slot: c.uri for c in changes}}
    acc2 = (yaml.safe_load(accounts) or {}).get("accounts") or []
    new_problems = (fleet_problems(acc2, links2, fleet.mode, mode_text)
                    - fleet_problems(fleet.accounts, links, fleet.mode, fleet.mode_text))
    if new_problems:
        sys.exit("ОШИБКА: результат не согласован — ничего не записываю:\n  " + "\n  ".join(sorted(new_problems)))
    return Rendered(env, accounts, mode_text, config)


def write_changes(changes: list[Change], fleet: Fleet, r: Rendered, *, state_dir: Path, presets: dict,
                  bad_ttl_hours: float, command: str, stamp: str) -> None:
    for path in (ENV_PATH, ACCOUNTS_PATH, fleet.mode_path, CONFIG_PATH):
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-attach-{stamp}"))

    # профили устройств (проверены в плане) — первыми: если упадёт, текстовые файлы ещё не тронуты
    for c in changes:
        if c.profile_op == "create":
            pg.create_profile(c.profile, state_dir, presets)
        elif c.profile_op == "reset":
            pg.reset_profile(c.slot, c.profile, state_dir, presets)
        else:
            pg.update_geo(c.slot, c.geo, state_dir, presets)

    ENV_PATH.write_text(r.env, encoding="utf-8")
    ags.write_inplace(ACCOUNTS_PATH, r.accounts)
    fleet.mode_path.write_text(r.mode_text, encoding="utf-8")
    if r.config is not None:
        ags.write_inplace(CONFIG_PATH, r.config)

    # гео прокси -> кэш (rot.geo_for / демон берут страну отсюда)
    cache = ags.read_json(GEO_CACHE_PATH)
    for c in changes:
        if gmc.link_kind(c.uri) == "proxy" and c.geo.get("country"):
            cache[c.proxy_name] = str(c.geo["country"]).upper()
    if cache:
        write_json_atomic(GEO_CACHE_PATH, cache)

    # чёрный список + история; идентичность в реестре демона сбрасываем (ensure_seeds заведёт новую)
    st = load_pa_state()
    now = time.time()
    ident = ags.read_json(IDENT_PATH) if IDENT_PATH.exists() else None
    for c in changes:
        st["history"].append({"ts": now, "command": command, "account": c.slot, "node": c.proxy_name,
                              "profile": c.profile["impersonate"], "reason": c.reason})
        if c.old_key:
            entry = {"ts": now, "ttl_hours": bad_ttl_hours, "reason": c.reason or "replaced", "account": c.slot}
            st["bad"][c.old_key] = entry
            if ident is not None:
                ident.setdefault("bad_nodes", {})[c.old_key] = dict(entry)
        if ident is not None and c.slot in (ident.get("slots") or {}):
            ident["slots"][c.slot]["active"] = None
    st["history"] = st["history"][-300:]
    write_json_atomic(PA_STATE_PATH, st)
    if ident is not None:
        write_json_atomic(IDENT_PATH, ident)

    rot.sync_source_pauses(ROOT)
    print(f"\nOK: записано (бэкапы *.bak-attach-{stamp}): vless_accounts.env, accounts.yaml, "
          f"scripts/{fleet.mode_path.name}{', config.yaml' if r.config is not None else ''}, state/profiles/")


def apply_changes(changes: list[Change], fleet: Fleet, links: dict[str, str], *, enabled: bool, presets: dict,
                  bad_ttl_hours: float, command: str) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rendered = render_changes(changes, fleet, links, enabled=enabled, stamp=stamp)   # ошибка здесь = ничего не записано
    write_changes(changes, fleet, rendered, state_dir=STATE_DIR, presets=presets, bad_ttl_hours=bad_ttl_hours,
                  command=command, stamp=stamp)


# ---------------------------------------------------------------- #
#  Команды
# ---------------------------------------------------------------- #

def load_context(args):
    ropts = load_rotation_cfg()
    pf = resolve_proxies_file(args.proxies_file, ropts)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    if not set(sources) <= {"proxy", "vless"} or not sources:
        sys.exit("ОШИБКА: --sources — proxy, vless или proxy,vless")
    return ropts, pf, sources


def ranked_free(args, ropts: dict, pf: Path, sources: list[str], links: dict, ident: dict, pa_state: dict):
    free = collect_free(pf, sources, links, ident, pa_state, ropts)
    print(f"Свободных узлов (не назначены, не в реестре ротации, не в чёрном списке): {len(free)}"
          f"  [{pf.name}; источники: {','.join(sources)}]")
    alive, stats = measure(free, ropts, args.probe_url, args.timeout_ms, args.workers, probe=not args.no_probe)
    print_stats(stats, alive)
    return alive


def build_change(slot: str, f: "ags.Free", taken_names: set[str], profile_op: str, *, geo: dict, warn: str | None,
                 presets: dict, taken_fps: set[str], avoid_imps: set[str], seed: int,
                 existing_profile: dict | None, no_mobile: bool = True, **kw) -> Change:
    uri, pname = ags.unique_link(f.node.uri, taken_names)
    ch_warn = [warn] if warn else []
    if profile_op == "geo":
        profile = profile_with_geo(existing_profile, geo)
    else:
        profile = fresh_profile(slot, geo, presets, taken_fps, avoid_imps, seed, no_mobile)
    errs = pg.validate_profile(profile, presets)
    if errs:
        sys.exit(f"ОШИБКА: профиль {slot} не прошёл проверку: {'; '.join(errs)}")
    if profile.get("geo_note"):
        ch_warn.append(str(profile["geo_note"]))
    return Change(slot=slot, node=f, uri=uri, proxy_name=pname, geo=geo, profile=profile,
                  profile_op=profile_op, warnings=ch_warn, **kw)


def geos_for(chosen: list["ags.Free"], live: bool, workers: int = 8) -> list[tuple[dict, str | None]]:
    with ThreadPoolExecutor(max(1, workers)) as ex:
        return list(ex.map(lambda f: geo_for_node(f, live), chosen))


def pick_with_geo(alive: list["ags.Free"], need: int, live: bool, workers: int = 8):
    """Первые `need` узлов очереди, у которых определилась страна. Без страны профиль не собрать
    (locale/timezone должны совпадать с выходным IP), поэтому такие узлы пропускаются и берутся следующие
    по скорости. -> (узлы, [(geo, предупреждение)])."""
    chosen, geos, skipped, i = [], [], 0, 0
    while len(chosen) < need and i < len(alive):
        batch = alive[i:i + (need - len(chosen))]
        i += len(batch)
        for f, (geo, warn) in zip(batch, geos_for(batch, live, workers)):
            if geo.get("country"):
                chosen.append(f)
                geos.append((geo, warn))
            else:
                skipped += 1
    if skipped:
        print(f"Пропущено прокси без определённой страны: {skipped} (запрос выходного IP через них не прошёл) — "
              "взяты следующие по скорости.")
    return chosen, geos


NO_COUNTRY_MSG = ("Ни у одного прокси не удалось определить страну: проверьте, что через прокси открываются "
                  "ipinfo.io / ip-api.com, либо задайте страну в имени строки (user:pass@host:port#us1).")


def cmd_rank(args) -> int:
    ropts, pf, sources = load_context(args)
    links, ident, pa = read_links(), ags.read_json(IDENT_PATH), load_pa_state()
    alive = ranked_free(args, ropts, pf, sources, links, ident, pa)
    for i, f in enumerate(alive[:args.top], 1):
        print(f"{i:>3}. {f.mihomo_name:<28} {f.delay if f.delay is not None else '?'} мс")
    return 0 if alive else 1


def cmd_add(args) -> int:
    if args.count < 0:
        sys.exit("ОШИБКА: --count должен быть >= 0")
    ropts, pf, sources = load_context(args)
    fleet = load_fleet(args.mode)
    accounts, by_name = fleet.accounts, fleet.by_name
    links, ident, pa = read_links(), ags.read_json(IDENT_PATH), load_pa_state()

    missing = sorted((n for n in fleet.members if n in by_name and n not in links), key=ags.acc_num)
    want = len(missing) + args.count
    if not want:
        print(f"Новых аккаунтов не нужно (--count 0, у всех слотов режима {args.mode} есть узел).")
        return 0
    alive = ranked_free(args, ropts, pf, sources, links, ident, pa)
    take = min(want, max(0, len(alive) - max(0, args.spare)))
    if take < want:
        print(f"ВНИМАНИЕ: нужно {want} узлов, доступно {take} — создаю {take}.")
    if take == 0:
        sys.exit("Свободных годных узлов нет — ничего не делаю.")
    chosen, geos = pick_with_geo(alive, take, not args.no_geo)
    take = len(chosen)
    if take == 0:
        sys.exit(NO_COUNTRY_MSG)

    n_missing = min(len(missing), take)
    new_start = max((ags.acc_num(a["name"]) for a in accounts), default=0) + 1
    new_slots = [f"account_{new_start + i}" for i in range(take - n_missing)]
    ports = ags.allocate_ports({int(a["proxy_port"]) for a in accounts if a.get("proxy_port")}, len(new_slots))

    presets = pg.load_presets()
    taken_fps = existing_fingerprints(STATE_DIR)
    used_imps = {a.get("impersonate") for a in accounts if a.get("impersonate")}
    names = taken_names_of(links)
    seed = args.seed if args.seed is not None else int(time.time())
    changes: list[Change] = []
    for i, (f, (geo, warn)) in enumerate(zip(chosen, geos)):
        if i < n_missing:
            slot, kw = missing[i], {}
        else:
            slot, kw = new_slots[i - n_missing], {"is_new": True, "port": ports[i - n_missing]}
        cur = pg.load_profile(slot, STATE_DIR)
        # профиль уже есть: у существующего слота оставляем (обновляем гео), у нового (остатки) — заменяем
        op = ("reset" if kw else "geo") if cur else "create"
        changes.append(build_change(slot, f, names, op, geo=geo, warn=warn, presets=presets, taken_fps=taken_fps,
                                    avoid_imps=used_imps, seed=seed, existing_profile=cur,
                                    no_mobile=not args.allow_mobile, **kw))
        used_imps.add(changes[-1].profile["impersonate"])

    print_plan(changes, by_name, args.mode)
    if not args.apply:
        print("\n[план] ничего не записано. Для записи добавь --apply")
        return 0
    apply_changes(changes, fleet, links, enabled=not args.disabled, presets=presets,
                  bad_ttl_hours=DEFAULT_BAD_TTL_HOURS, command="add")
    next_steps(changes, replaced=False, mode=args.mode)
    return 0


def auto_detect(targets_pool: list[str], links: dict[str, str], args) -> dict[str, str]:
    """Аккаунты, у которых Reddit через ТЕКУЩИЙ прокси недоступен. -> {аккаунт: причина}."""
    prox = [(a, links[a]) for a in targets_pool if a in links and gmc.link_kind(links[a]) == "proxy"]
    bad: dict[str, str] = {}
    with ThreadPoolExecutor(max(1, args.workers)) as ex:
        for (acc, _), (ms, why) in zip(prox, ex.map(lambda al: probe_proxy(al[1], args.probe_url,
                                                                           args.timeout_ms / 1000), prox)):
            if ms is None:
                bad[acc] = f"Reddit недоступен: {why}"
    skipped = len(targets_pool) - len(prox)
    if skipped:
        print(f"--auto: {skipped} аккаунт(ов) на VLESS/без узла не проверялись (укажите их через --accounts).")
    return bad


def cmd_replace(args) -> int:
    ropts, pf, sources = load_context(args)
    fleet = load_fleet(args.mode)
    by_name = fleet.by_name
    links, ident, pa = read_links(), ags.read_json(IDENT_PATH), load_pa_state()
    if not (args.accounts or args.from_hub or args.auto):
        sys.exit("ОШИБКА: укажите --accounts ..., --from-hub или --auto")

    reasons: dict[str, str] = {}
    for a in parse_accounts(args.accounts or [], list(by_name)):
        reasons[a] = args.reason
    if args.from_hub:
        for a in hub_captcha_accounts():
            reasons.setdefault(a, "капча (login_hub)")
    if args.auto:
        for a, why in auto_detect(sorted(fleet.members, key=ags.acc_num), links, args).items():
            reasons.setdefault(a, why)
    targets = sorted(reasons, key=ags.acc_num)
    outsiders = [a for a in targets if a not in fleet.members]
    if outsiders:
        other = "guest" if args.mode == "login" else "login"
        sys.exit(f"ОШИБКА: {', '.join(outsiders)} — не аккаунты режима {args.mode} "
                 f"(для других попробуйте --mode {other}; гостевую ноду также меняет rotation-daemon)")
    if args.mode == "login" and not args.force:
        with_session = [a for a in targets if has_session(a)]
        if with_session:
            print(f"Пропущены (уже есть сохранённая сессия входа — замена прокси и профиля её сбросит): "
                  f"{', '.join(with_session)}. Если это нужно — добавьте --force.")
            targets = [a for a in targets if a not in with_session]
    if not targets:
        print("Нечего заменять: аккаунты не отмечены (капчи в login_hub нет, --auto ничего не нашёл) "
              "или у них есть сессия.")
        return 0
    print(f"К замене: {', '.join(f'{a} ({reasons[a]})' for a in targets)}")

    alive = ranked_free(args, ropts, pf, sources, links, ident, pa)
    if len(alive) < len(targets):
        print(f"ВНИМАНИЕ: свободных годных узлов {len(alive)} < {len(targets)} аккаунтов — "
              f"заменю первые {len(alive)}.")
    targets = targets[:len(alive)]
    if not targets:
        sys.exit("Свободных годных узлов нет — ничего не делаю.")
    chosen, geos = pick_with_geo(alive, len(targets), not args.no_geo)
    targets = targets[:len(chosen)]
    if not targets:
        sys.exit(NO_COUNTRY_MSG)

    presets = pg.load_presets()
    taken_fps = existing_fingerprints(STATE_DIR)       # включая старый профиль слота: устройство будет другим
    names = taken_names_of(links)
    seed = args.seed if args.seed is not None else int(time.time())
    changes: list[Change] = []
    for slot, f, (geo, warn) in zip(targets, chosen, geos):
        old_link = links.get(slot)
        cur = pg.load_profile(slot, STATE_DIR)
        old_imp = (cur or {}).get("impersonate") or by_name[slot].get("impersonate")
        changes.append(build_change(
            slot, f, names, "reset" if cur else "create", geo=geo, warn=warn, presets=presets, taken_fps=taken_fps,
            avoid_imps={old_imp} if old_imp else set(), seed=seed, existing_profile=cur, no_mobile=not args.allow_mobile,
            old_key=rot.node_key(old_link) if old_link else None, reason=reasons[slot]))

    print_plan(changes, by_name, args.mode)
    if not args.apply:
        print("\n[план] ничего не записано. Для записи добавь --apply")
        return 0
    apply_changes(changes, fleet, links, enabled=True, presets=presets,
                  bad_ttl_hours=args.bad_ttl_hours, command="replace")
    print("Старые cookies остались от прежнего IP/устройства — войдите в аккаунт заново.")
    next_steps(changes, replaced=True, mode=args.mode)
    return 0


def cmd_reprofile(args) -> int:
    """Пересоздать профиль устройства БЕЗ смены прокси (например, мобильный -> десктоп). Гео прежнее."""
    fleet = load_fleet(args.mode)
    targets = parse_accounts(args.accounts, list(fleet.by_name))
    outsiders = [a for a in targets if a not in fleet.members]
    if outsiders:
        sys.exit(f"ОШИБКА: {', '.join(outsiders)} — не аккаунты режима {args.mode}")
    if args.mode == "login" and not args.force:
        with_session = [a for a in targets if has_session(a)]
        if with_session:
            print(f"Пропущены (есть сохранённая сессия входа — смена устройства её сбросит): "
                  f"{', '.join(with_session)}. Если это нужно — добавьте --force.")
            targets = [a for a in targets if a not in with_session]
    presets = pg.load_presets()
    taken_fps = existing_fingerprints(STATE_DIR)
    seed = args.seed if args.seed is not None else int(time.time())
    rows = []
    for slot in targets:
        cur = pg.load_profile(slot, STATE_DIR)
        if cur is None:
            print(f"{slot}: профиля устройства нет — пропуск (его создаёт add/replace или scripts/profile.py --create)")
            continue
        if not args.all and not (cur.get("mobile") and not args.allow_mobile):
            print(f"{slot}: профиль уже подходит ({cur['os']}, {cur['impersonate']}) — пропуск (--all пересоздаст любой)")
            continue
        geo = {k: cur[k] for k in pg.GEO_FIELDS if k in cur}
        new = fresh_profile(slot, geo, presets, taken_fps, {cur["impersonate"]}, seed, not args.allow_mobile)
        errs = pg.validate_profile(new, presets)
        if errs:
            sys.exit(f"ОШИБКА: профиль {slot} не прошёл проверку: {'; '.join(errs)}")
        rows.append((slot, cur, new))
    if not rows:
        print("Нечего пересоздавать.")
        return 0
    print(f"\nРежим: {args.mode}\n{'слот':<11} {'было':<26} {'станет':<26} гео")
    for slot, cur, new in rows:
        print(f"{slot:<11} {cur['os'] + ' ' + cur['impersonate']:<26} {new['os'] + ' ' + new['impersonate']:<26} "
              f"{new['locale']} (прокси не меняется)")
    if not args.apply:
        print("\n[план] ничего не записано. Для записи добавь --apply")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    accounts = fleet.accounts_text
    for slot, cur, new in rows:
        accounts = patch_account_keys(accounts, slot, {"impersonate": new["impersonate"], "user_agent": new["user_agent"]})
    mode_text = fleet.mode_text
    if fleet.mode == "guest":
        plans = [ags.SlotPlan(slot, None, "", "", {"locale": new["locale"], "timezone_id": new["timezone_id"]},
                              False, None, guest_profile_view(new)) for slot, _, new in rows]
        mode_text = ags.patch_guest_yaml(fleet.mode_text, plans)       # SystemExit до любой записи
    acc2 = (yaml.safe_load(accounts) or {}).get("accounts") or []
    if len(acc2) != len(fleet.accounts):
        sys.exit("ОШИБКА: после правки accounts.yaml число аккаунтов изменилось — ничего не записываю")
    for path in (ACCOUNTS_PATH, fleet.mode_path):
        shutil.copy2(path, path.with_name(f"{path.name}.bak-attach-{stamp}"))
    for slot, _, new in rows:
        pg.reset_profile(slot, new, STATE_DIR, presets)
    ags.write_inplace(ACCOUNTS_PATH, accounts)
    if mode_text != fleet.mode_text:
        fleet.mode_path.write_text(mode_text, encoding="utf-8")
    ident = ags.read_json(IDENT_PATH) if IDENT_PATH.exists() else None
    if ident is not None:
        for slot, _, _ in rows:
            if slot in (ident.get("slots") or {}):
                ident["slots"][slot]["active"] = None
        write_json_atomic(IDENT_PATH, ident)
    print(f"\nOK: профили пересозданы (бэкапы *.bak-attach-{stamp}; старые профили — state/profiles/.history/).")
    print("Дальше: закройте старые окна этих аккаунтов в login_hub и запустите заново "
          f"(venv/bin/python3 scripts/login_hub.py --accounts {' '.join(r[0] for r in rows)}); "
          "для работающего скрапера — docker compose up -d --build scraper.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--mode", choices=("login", "guest"), default="login",
                       help="login (по умолчанию): логин-слоты, enabled: false до входа; guest: гостевые слоты")
        p.add_argument("--proxies-file", default=None, help=f"файл с прокси (по умолчанию {DEFAULT_PROXIES_FILE})")
        p.add_argument("--sources", default="proxy,vless", help="откуда брать узлы: proxy, vless или proxy,vless")
        p.add_argument("--probe-url", default=REDDIT_URL)
        p.add_argument("--timeout-ms", type=int, default=8000)
        p.add_argument("--workers", type=int, default=32)
        p.add_argument("--no-probe", action="store_true", help="не проверять узлы (порядок как в файле)")
        p.add_argument("--no-geo", action="store_true", help="не определять страну по выходному IP прокси")
        p.add_argument("--seed", type=int, default=None, help="seed генератора профилей (по умолчанию — время)")
        p.add_argument("--allow-mobile", action="store_true",
                       help="разрешить мобильные профили (Android/iOS); по умолчанию — только десктоп")
        p.add_argument("--apply", action="store_true", help="записать (без флага — только план)")

    p_add = sub.add_parser("add", help="новые аккаунты + прокси")
    common(p_add)
    p_add.add_argument("--count", type=int, default=1, help="сколько НОВЫХ аккаунтов создать")
    p_add.add_argument("--spare", type=int, default=0, help="сколько годных узлов оставить про запас")
    p_add.add_argument("--disabled", action="store_true", help="guest: создать аккаунты с enabled: false")
    p_add.set_defaults(fn=cmd_add)

    p_rep = sub.add_parser("replace", help="сменить профиль и прокси у аккаунтов с капчей/недоступным Reddit")
    common(p_rep)
    p_rep.add_argument("--accounts", nargs="*", help="account_11 | 11 | 11-15")
    p_rep.add_argument("--from-hub", action="store_true", help="аккаунты, у которых login_hub ждёт из-за капчи")
    p_rep.add_argument("--auto", action="store_true", help="проверить доступность Reddit через текущие прокси")
    p_rep.add_argument("--reason", default="капча / Reddit недоступен (вручную)")
    p_rep.add_argument("--bad-ttl-hours", type=float, default=DEFAULT_BAD_TTL_HOURS,
                       help="на сколько часов старый прокси не выдавать снова")
    p_rep.add_argument("--force", action="store_true",
                       help="login: заменять и аккаунты с сохранённой сессией (сессия слетит)")
    p_rep.set_defaults(fn=cmd_replace)

    p_rep2 = sub.add_parser("reprofile", help="пересоздать профиль устройства без смены прокси")
    common(p_rep2)
    p_rep2.add_argument("--accounts", nargs="+", required=True, help="account_58 | 58 | 56-65")
    p_rep2.add_argument("--all", action="store_true", help="пересоздать любой профиль (по умолчанию — только мобильные)")
    p_rep2.add_argument("--force", action="store_true", help="login: и аккаунты с сохранённой сессией (сессия слетит)")
    p_rep2.set_defaults(fn=cmd_reprofile)

    p_rank = sub.add_parser("rank", help="показать очередь прокси по скорости")
    common(p_rank)
    p_rank.add_argument("--top", type=int, default=30)
    p_rank.set_defaults(fn=cmd_rank)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
