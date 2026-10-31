#!/usr/bin/env python3
"""
scripts/reset_slots.py — убрать ВСЕ аккаунты-слоты, чтобы завести их заново (add_login_slots.py).

По умолчанию — ТОЛЬКО план (что будет убрано). Запись — с --apply. Ничего не удаляется безвозвратно:
бэкапы текстовых файлов — *.bak-reset-<время>, а per-account файлы (cookies, storage_state, state) ПЕРЕНОСЯТСЯ в
state/reset-<время>/ (иначе новый account_1 подхватил бы куки и профиль старого).

Что убирается:
  accounts.yaml                    все записи accounts: (шапка-комментарий остаётся; пишется в тот же inode —
                                   файл смонтирован в контейнер)
  vless_accounts.env               все строки account_N = ... (комментарии остаются)
  scripts/refresh_cookies.yaml     login_accounts и account_geo
  scripts/refresh_guest_cookies.yaml  guest_accounts и гео слотов
  config.yaml                      rotation.slots
  cookies/, storage_state/         account_*.json
  state/                           identities.json (реестр нод — иначе он держит ноды занятыми), slots/, requests/,
                                   check_login/, profiles/, human/, runtime_disabled.json, node_source_disabled.json
Не трогаются: пул нод (all_nodes*.txt), rotation_state.json (список плохих нод), mihomo_config.yaml (пересоберётся).

Дальше (по одному аккаунту на каждую годную ноду, без запаса):

    venv/bin/python3 scripts/reset_slots.py --apply
    venv/bin/python3 scripts/sync_subscription.py --apply      # свежий пул нод (если используешь подписку)
    venv/bin/python3 scripts/filter_nodes.py --write           # годные ноды -> all_nodes.reddit_ok.txt
    venv/bin/python3 scripts/add_login_slots.py --spare 0 --apply
    venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo

Перед --apply останови скрапер и демон (docker compose stop scraper; sudo systemctl stop rotation-daemon).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import login_fleet as lf  # noqa: E402
import rotation as rot  # noqa: E402

STATE_ITEMS = ("identities.json", "slots", "requests", "check_login", "profiles", "human",
               "runtime_disabled.json", "node_source_disabled.json")


def strip_accounts_yaml(text: str) -> str:
    """Убирает записи списка accounts: (от первого `- name:` до следующей секции); комментарии шапки остаются."""
    lines = text.splitlines(keepends=True)
    first = next((i for i, l in enumerate(lines) if re.match(r"^\s*-\s*name:", l)), None)
    if first is None:
        return text
    end = next((i for i in range(first, len(lines)) if re.match(r"^[A-Za-z_]", lines[i])), len(lines))
    return "".join(lines[:first] + lines[end:])


def strip_env(text: str) -> str:
    return "".join(l for l in text.splitlines(keepends=True) if not re.match(r"^\s*account_\d+\s*=", l))


def strip_login_yaml(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out, section = [], None
    for l in lines:
        if re.match(r"^[A-Za-z_]", l):
            section = re.match(r"^([A-Za-z_]+):", l).group(1) if re.match(r"^([A-Za-z_]+):", l) else None
        elif section == "login_accounts" and re.match(r"^\s+-\s*account_\d+\s*(#.*)?$", l):
            continue
        elif section == "account_geo" and re.match(r"^\s+account_\d+:", l):
            continue
        out.append(l)
    return "".join(out)


def slot_names(p: "lf.Paths") -> set[str]:
    names: set[str] = set()
    if p.accounts.exists():
        for a in (yaml.safe_load(p.accounts.read_text(encoding="utf-8")) or {}).get("accounts") or []:
            if a.get("name"):
                names.add(a["name"])
    if p.env.exists():
        names |= set(re.findall(r"(?m)^\s*(account_\d+)\s*=", p.env.read_text(encoding="utf-8")))
    for path, key in ((p.login, "login_accounts"), (p.guest, "guest_accounts")):
        if path.exists():
            v = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(key) or []
            names |= {str(x) for x in (v if isinstance(v, list) else list(v))}
    if p.config.exists():
        slots = ((yaml.safe_load(p.config.read_text(encoding="utf-8")) or {}).get("rotation") or {}).get("slots") or []
        names |= {str(x) for x in slots}
    return {n for n in names if re.fullmatch(r"account_\d+", n)}


def reset(root: Path, *, apply: bool, out=print) -> int:
    p = lf.Paths(root)
    names = slot_names(p)
    out(f"Слотов к удалению: {len(names)}" + (f" ({', '.join(sorted(names, key=lf.acc_num)[:6])}…)" if names else ""))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    arch = p.state / f"reset-{stamp}"

    edits = []   # (path, new_text, inplace)
    if p.accounts.exists():
        edits.append((p.accounts, strip_accounts_yaml(p.accounts.read_text(encoding="utf-8")), True))
    if p.env.exists():
        edits.append((p.env, strip_env(p.env.read_text(encoding="utf-8")), False))
    if p.login.exists():
        edits.append((p.login, strip_login_yaml(p.login.read_text(encoding="utf-8")), False))
    if p.guest.exists():
        edits.append((p.guest, lf.remove_guest_slots(p.guest.read_text(encoding="utf-8"), sorted(names)), False))
    if p.config.exists():
        edits.append((p.config, lf.remove_config_slots(p.config.read_text(encoding="utf-8"), sorted(names)), True))

    moves = []   # (src, dst)
    for sub, pat in (("cookies", "account_*.json"), ("storage_state", "account_*.json")):
        for f in sorted((root / sub).glob(pat)) if (root / sub).is_dir() else []:
            moves.append((f, arch / sub / f.name))
    for item in STATE_ITEMS:
        f = p.state / item
        if f.exists():
            moves.append((f, arch / "state" / item))

    edits = [(path, new, inplace) for path, new, inplace in edits if new != path.read_text(encoding="utf-8")]
    for path, _, _ in edits:
        out(f"  {path.relative_to(root)}: изменится")
    if not edits and not moves:
        out("Нечего убирать — слотов уже нет.")
        return 0
    out(f"  файлов/каталогов в архив {arch.relative_to(root)}: {len(moves)}")
    if not apply:
        out("\n[план] ничего не изменено. Остановите скрапер и демон, затем --apply")
        return 0

    for path, new, inplace in edits:
        shutil.copy2(path, path.with_name(f"{path.name}.bak-reset-{stamp}"))
        (rot._write_inplace if inplace else rot._atomic_write)(path, new)
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    out(f"OK: слоты убраны (бэкапы *.bak-reset-{stamp}, архив {arch.relative_to(root)}).")
    out("Дальше: add_login_slots.py --spare 0 --apply (см. шапку скрипта).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="выполнить (без флага — только план)")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    return reset(Path(args.root) if args.root else rot.ROOT, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
