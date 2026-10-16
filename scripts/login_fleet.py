"""
scripts/login_fleet.py — общие функции «парка из логин-аккаунтов» (модуль 01):
пути проекта, fleet-конфиг, текстовая правка yaml-файлов (комментарии
сохраняются), проверка согласованности парка, статус входа слота.

Используют: convert_to_login.py, add_login_slots.py, rotation_daemon.py,
manual_login.py, filter_nodes.py и тесты. Модуль ничего не пишет сам —
только возвращает новый текст / результат проверки.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

# Новые поля config.yaml -> fleet.* (см. модуль 01)
FLEET_DEFAULTS = {
    "reserve_nodes": 6,               # годных нод, которые НЕ раздаются слотам (запас на замену)
    "min_good_nodes_warn": 10,        # меньше годных нод — предупреждение в отчёте/статусе
    "node_check_interval_hours": 24,  # как часто filter_nodes.py перепроверяет пул (таймер)
    "node_replace_only": True,        # сгоревшая нода логин-слота = только замена ноды (без смены профиля/сессии)
    "guest_enabled": False,           # гостевой харвест/ротация: код остаётся, но выключен
    "use_reddit_ok_file": True,       # кандидатов на замену брать только из all_nodes.reddit_ok.txt (если он есть)
}

FIRST_LOGIN_PORT = 7946


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def accounts(self) -> Path:
        return self.root / "accounts.yaml"

    @property
    def config(self) -> Path:
        return self.root / "config.yaml"

    @property
    def guest(self) -> Path:
        return self.root / "scripts" / "refresh_guest_cookies.yaml"

    @property
    def login(self) -> Path:
        return self.root / "scripts" / "refresh_cookies.yaml"

    @property
    def env(self) -> Path:
        return self.root / "vless_accounts.env"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def ident(self) -> Path:
        return self.state / "identities.json"

    @property
    def node_report(self) -> Path:
        return self.state / "node_report.json"


def acc_num(name: str) -> int:
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def parse_slot_spec(spec: str) -> list[str]:
    """'11-55', '11-15,20,30-31' -> ['account_11', ...] (по возрастанию, без дублей)."""
    nums: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(?:account_)?(\d+)(?:-(?:account_)?(\d+))?", part)
        if not m:
            raise ValueError(f"не разобрал диапазон слотов: {part!r} (ждал 11-55 или 11,12,20)")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if b < a:
            raise ValueError(f"диапазон наоборот: {part!r}")
        nums.update(range(a, b + 1))
    return [f"account_{n}" for n in sorted(nums)]


def load_fleet_cfg(config_path: Path) -> dict:
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    return {**FLEET_DEFAULTS, **(data.get("fleet") or {})}


def load_links(p: "Paths") -> dict[str, str]:
    """account_N -> vless://... из vless_accounts.env проекта p (generate_mihomo_config
    читает путь из модульной константы — подменяем её на время вызова)."""
    import generate_mihomo_config as gmc

    old = gmc.VLESS_ENV_PATH
    gmc.VLESS_ENV_PATH = p.env
    try:
        return gmc.load_vless_links()
    finally:
        gmc.VLESS_ENV_PATH = old


def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def login_account_names(login_yaml: dict, accounts: list[dict]) -> set[str]:
    """Логин-слоты: явный список login_accounts + аккаунты с login: true."""
    return set(login_yaml.get("login_accounts") or []) | {a["name"] for a in accounts if a.get("login")}


# ---------------------------------------------------------------- #
#  Текстовая правка accounts.yaml
# ---------------------------------------------------------------- #

def _block_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    start = next((i for i, l in enumerate(lines)
                  if re.match(rf"^\s*-\s*name:\s*{re.escape(name)}\s*$", l)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*-\s*name:", lines[i])), len(lines))
    return start, end


def set_account_key(text: str, name: str, key: str, value: str, after: tuple[str, ...] = ("cookie_file", "enabled")) -> str:
    """Ставит `key: value` в блоке account `name`: заменяет строку, а если её
    нет — вставляет после первой найденной из `after` (иначе в конец блока)."""
    lines = text.splitlines(keepends=True)
    b = _block_bounds(lines, name)
    if b is None:
        return text
    start, end = b
    for i in range(start + 1, end):
        m = re.match(rf"^(\s+){re.escape(key)}:\s*", lines[i])
        if m:
            lines[i] = f"{m.group(1)}{key}: {value}\n"
            return "".join(lines)
    indent = "    "
    for i in range(start + 1, end):
        m = re.match(r"^(\s+)\w+:", lines[i])
        if m:
            indent = m.group(1)
            break
    for anchor in after:
        for i in range(start + 1, end):
            if re.match(rf"^\s+{re.escape(anchor)}:\s*", lines[i]):
                lines.insert(i + 1, f"{indent}{key}: {value}\n")
                return "".join(lines)
    last = end
    while last > start + 1 and lines[last - 1].strip() == "":
        last -= 1
    lines.insert(last, f"{indent}{key}: {value}\n")
    return "".join(lines)


def to_login_account(text: str, name: str, enabled: bool = False) -> str:
    """Слот -> логин-режим: enabled, login: true, storage_state по соглашению."""
    text = set_account_key(text, name, "enabled", "true" if enabled else "false")
    text = set_account_key(text, name, "login", "true", after=("cookie_file", "enabled"))
    text = set_account_key(text, name, "storage_state", f"storage_state/{name}.json", after=("login", "cookie_file"))
    return text


def _yq(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def login_account_block(name: str, port: int, locale: str, impersonate: str, user_agent: str,
                        enabled: bool = False) -> str:
    return (
        f"  - name: {name}\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        f"    login: true\n"
        f"    cookie_file: cookies/{name}.json\n"
        f"    storage_state: storage_state/{name}.json\n"
        f"    proxy_port: {port}\n"
        f"    locale: {locale}\n"
        f"    impersonate: {impersonate}\n"
        f"    user_agent: {_yq(user_agent)}\n"
    )


# ---------------------------------------------------------------- #
#  guest yaml / config.yaml / login yaml
# ---------------------------------------------------------------- #

def remove_guest_slots(text: str, slots: list[str]) -> str:
    """Убирает слоты из guest_accounts и их строки в account_geo (шапка файла).
    guest_accounts должна быть последней секцией (как и в add_guest_slots)."""
    m = re.search(r"(?m)^guest_accounts:[ \t]*(?:\{\})?[ \t]*\n?", text)
    if not m:
        raise ValueError("в refresh_guest_cookies.yaml нет секции guest_accounts:")
    if re.search(r"(?m)^[A-Za-z_]", text[m.end():]):
        raise ValueError("после guest_accounts: есть другие секции — скрипт рассчитан на то, что она последняя")
    head = text[:m.start()]
    guest = (yaml.safe_load(text) or {}).get("guest_accounts") or {}
    for s in slots:
        guest.pop(s, None)
        head = re.sub(rf"(?m)^[ \t]*{re.escape(s)}:[ \t]*\{{[^}}]*\}}.*\n?", "", head)
    ordered = {k: guest[k] for k in sorted(guest, key=acc_num)}
    tail = yaml.safe_dump({"guest_accounts": ordered}, allow_unicode=True, sort_keys=False, width=200)
    return head + tail


def remove_config_slots(text: str, slots: list[str]) -> str:
    """Убирает слоты из rotation.slots (блочный список в config.yaml)."""
    drop = set(slots)
    lines = text.splitlines(keepends=True)
    r = next((i for i, l in enumerate(lines) if re.match(r"^rotation:\s*(#.*)?$", l)), None)
    if r is None:
        return text
    s = next((i for i in range(r + 1, len(lines)) if re.match(r"^  slots:\s*(\[\s*\])?\s*(#.*)?$", lines[i])), None)
    if s is None:
        return text
    out = lines[:s + 1]
    i = s + 1
    while i < len(lines):
        m = re.match(r"^    -\s*(account_\d+)\s*(#.*)?$", lines[i])
        if m:
            if m.group(1) not in drop:
                out.append(lines[i])
        elif lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            out.append(lines[i])
        else:
            break
        i += 1
    out.extend(lines[i:])
    new = "".join(out)
    # пустой список слотов: `slots:` без элементов YAML прочтёт как null
    try:
        empty = ((yaml.safe_load(new) or {}).get("rotation") or {}).get("slots") is None
    except yaml.YAMLError:
        empty = False
    if empty:
        new = re.sub(r"(?m)^(  slots:)[ \t]*(#.*)?$", r"\1 []", new, count=1)
    return new


def patch_login_yaml(text: str, slots: list[str], geo: dict[str, dict], labels: dict[str, str] | None = None,
                     overwrite_geo: set[str] | None = None) -> str:
    """Добавляет слоты в login_accounts и их гео в account_geo (refresh_cookies.yaml).
    overwrite_geo — слоты, у которых уже существующую строку гео надо заменить (сменилась нода)."""
    labels = labels or {}
    overwrite_geo = overwrite_geo or set()
    lines = text.splitlines(keepends=True)

    # --- login_accounts ---
    la = next((i for i, l in enumerate(lines) if re.match(r"^login_accounts:\s*(#.*)?$", l)), None)
    if la is None:
        raise ValueError("в refresh_cookies.yaml нет секции login_accounts:")
    last, have = la, set()
    for i in range(la + 1, len(lines)):
        m = re.match(r"^\s+-\s*(account_\d+)\s*(#.*)?$", lines[i])
        if m:
            have.add(m.group(1))
            last = i
        elif lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            continue
        else:
            break
    for off, n in enumerate(s for s in slots if s not in have):
        lines.insert(last + 1 + off, f"  - {n}\n")

    # --- account_geo ---
    ag = next((i for i, l in enumerate(lines) if re.match(r"^account_geo:\s*(#.*)?$", l)), None)
    if ag is None:
        lines.append("\naccount_geo:\n")
        ag = len(lines) - 1
    have_geo, last = set(), ag
    for i in range(ag + 1, len(lines)):
        m = re.match(r"^\s+(account_\d+):", lines[i])
        if m:
            have_geo.add(m.group(1))
            last = i
        elif lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            continue
        else:
            break
    def geo_line(n: str) -> str:
        g = geo[n]
        tail = f"   # {labels[n]}" if labels.get(n) else ""
        return f'  {n}: {{locale: "{g["locale"]}", timezone_id: "{g["timezone_id"]}"}}{tail}\n'

    for i in range(ag + 1, len(lines)):
        m = re.match(r"^\s+(account_\d+):", lines[i])
        if m and m.group(1) in overwrite_geo and m.group(1) in geo:
            lines[i] = geo_line(m.group(1))
    add = [geo_line(n) for n in slots if n not in have_geo and n in geo]
    lines[last + 1:last + 1] = add
    return "".join(lines)


# ---------------------------------------------------------------- #
#  Проверка согласованности парка
# ---------------------------------------------------------------- #

def validate_fleet(accounts: list[dict], links: dict[str, str] | None = None, guest: dict | None = None,
                   login_cfg: dict | None = None, rotation_slots: list[str] | None = None) -> list[str]:
    """Список проблем (пустой = всё согласовано): дубли портов/нод/имён,
    слот сразу в двух режимах, логин-слот не в login_accounts, включённый
    логин-слот без cookie_file."""
    problems: list[str] = []
    names = [a["name"] for a in accounts]
    if len(names) != len(set(names)):
        problems.append("дубли имён аккаунтов в accounts.yaml")
    ports = [a["proxy_port"] for a in accounts if a.get("proxy_port")]
    dup_ports = sorted({p for p in ports if ports.count(p) > 1})
    if dup_ports:
        problems.append(f"дубли proxy_port: {dup_ports}")
    if links:
        keys: dict[str, list[str]] = {}
        for acc, link in links.items():
            keys.setdefault(link.split("#", 1)[0].strip(), []).append(acc)
        for k, accs in keys.items():
            if len(accs) > 1:
                problems.append(f"одна нода у нескольких слотов: {', '.join(sorted(accs, key=acc_num))}")
    guest = guest or {}
    both = sorted(set(guest) & {a["name"] for a in accounts if a.get("login")}, key=acc_num)
    if both:
        problems.append(f"слот одновременно гостевой и login: true: {', '.join(both)}")
    if login_cfg is not None:
        want = {a["name"] for a in accounts if a.get("login")}
        missing = sorted(want - set(login_cfg.get("login_accounts") or []), key=acc_num)
        if missing:
            problems.append(f"login: true, но нет в login_accounts (refresh_cookies.yaml): {', '.join(missing)}")
        geo = login_cfg.get("account_geo") or {}
        nogeo = sorted((n for n in want if n not in geo), key=acc_num)
        if nogeo:
            problems.append(f"нет account_geo для: {', '.join(nogeo)}")
    if rotation_slots:
        bad = sorted({s for s in rotation_slots if s in {a["name"] for a in accounts if a.get("login")}}, key=acc_num)
        if bad:
            problems.append(f"логин-слоты в rotation.slots (гостевой ротации): {', '.join(bad)}")
    return problems


# ---------------------------------------------------------------- #
#  Статус входа («нужен вход» / «нужна нода» / «проверить вход»)
# ---------------------------------------------------------------- #
#  needs_node  — внутреннее состояние демона (state/identities.json, slots[N].needs_node).
#  check_login — флаг-файл state/check_login/account_N: ставит демон после замены ноды,
#                снимают manual_login.py / refresh_cookies.py после подтверждённого входа.
#                Отдельным файлом, потому что демон перезаписывает identities.json целиком.

NEED_LOGIN = "нужен вход"
NEED_NODE = "нужна нода"
CHECK_LOGIN = "проверить вход"
LOGIN_OK = "ok"


def check_login_flag(state_dir: Path, name: str) -> Path:
    return state_dir / "check_login" / name


def set_check_login(state_dir: Path, name: str, reason: str = "") -> None:
    f = check_login_flag(state_dir, name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{time.time():.0f} {reason}\n", encoding="utf-8")
    for d in (f.parent, f):
        try:
            os.chmod(d, 0o777 if d.is_dir() else 0o666)
        except OSError:
            pass


def clear_check_login(state_dir: Path, name: str) -> bool:
    try:
        check_login_flag(state_dir, name).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def slot_flags(state_dir: Path, name: str) -> dict:
    """{'needs_node': bool, 'check_login': bool} — для списков и статуса."""
    needs = False
    try:
        st = json.loads((state_dir / "identities.json").read_text(encoding="utf-8"))
        needs = bool(((st.get("slots") or {}).get(name) or {}).get("needs_node"))
    except (OSError, ValueError):
        pass
    return {"needs_node": needs, "check_login": check_login_flag(state_dir, name).exists()}


def login_status(acc: dict, flags: dict | None = None) -> str:
    """Для логин-слота: нужен вход (выключен до первого входа), нужна нода (нода сгорела,
    замены пока нет), проверить вход (ноду заменили — IP сменился при той же сессии),
    иначе ok. Для гостя — пустая строка."""
    if not acc.get("login"):
        return ""
    flags = flags or {}
    if not acc.get("enabled"):
        return NEED_LOGIN
    if flags.get("needs_node"):
        return NEED_NODE
    if flags.get("check_login"):
        return CHECK_LOGIN
    return LOGIN_OK
