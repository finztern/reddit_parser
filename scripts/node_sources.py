#!/usr/bin/env python3
"""
scripts/node_sources.py — какие источники узлов включены: VLESS (all_nodes.txt), прокси (all_proxys.txt) или оба.

Источник задаётся тремя слоями (сильнее — выше):
  1. state/node_sources.json   — переключение «на лету» из панели (клавиша `n`) или этим скриптом;
  2. config.yaml: rotation.node_sources — список ["vless", "proxy"] (по умолчанию оба);
  3. оба источника.

Что делает переключатель (mihomo перезапускать НЕ нужно — в нём всегда загружены узлы обоих типов):
  * аккаунты, чей узел (vless_accounts.env) относится к выключенному источнику, ставятся на паузу —
    список лежит в state/node_source_disabled.json, его читает планировщик скрапера (рядом с
    runtime_disabled.json и fleet_polling.disabled_accounts) и подхватывает за секунды;
  * новые слоты (add_login_slots.py / add_guest_slots.py) и замена сгоревшего узла (rotation_daemon.py)
    берут узлы только из включённых источников.

    python3 scripts/node_sources.py                 # показать состояние
    python3 scripts/node_sources.py --set proxy     # только прокси  (vless | proxy | all)
    python3 scripts/node_sources.py --clear         # снять переключатель панели -> брать из config.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ALL = ("vless", "proxy")
STATE_FILE = "node_sources.json"
DISABLED_FILE = "node_source_disabled.json"
ACCOUNT_LINE_RE = re.compile(r"^\s*(account_\d+)\s*=\s*(\S+)\s*$")


def kind_of(uri: str) -> str:
    return "vless" if (uri or "").lstrip().lower().startswith("vless://") else "proxy"


def parse_sources(value) -> list[str]:
    """['vless'] / 'proxy' / 'all' / 'both' / 'vless,proxy' -> нормализованный список (порядок ALL).
    Пустое или мусорное значение -> оба источника (безопасный дефолт)."""
    if isinstance(value, str):
        value = [p for p in re.split(r"[,\s+]+", value.strip().lower()) if p]
    if not isinstance(value, (list, tuple, set)):
        return list(ALL)
    items = {str(x).strip().lower() for x in value}
    if items & {"all", "both"}:
        return list(ALL)
    got = [s for s in ALL if s in items]
    return got or list(ALL)


def config_sources(config_path: Path | None = None) -> list[str]:
    path = config_path or (ROOT / "config.yaml")
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return list(ALL)
    return parse_sources((cfg.get("rotation") or {}).get("node_sources"))


def runtime_sources(state_dir: Path) -> list[str] | None:
    try:
        data = json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "sources" not in data:
        return None
    return parse_sources(data["sources"])


def active_sources(root: Path | None = None, config_path: Path | None = None) -> list[str]:
    root = root or ROOT
    rt = runtime_sources(root / "state")
    if rt is not None:
        return rt
    return config_sources(config_path or (root / "config.yaml"))


def _atomic_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_links(env_path: Path) -> dict[str, str]:
    """account_N -> ссылка из vless_accounts.env (без валидации: нужен только тип)."""
    links: dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return links
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = ACCOUNT_LINE_RE.match(line)
        if m:
            links[m.group(1)] = m.group(2)
    return links


def sync_disabled(root: Path, sources: list[str]) -> list[str]:
    """Пересобрать state/node_source_disabled.json: аккаунты с узлом выключенного источника."""
    links = read_links(root / "vless_accounts.env")
    off = sorted((a for a, u in links.items() if kind_of(u) not in sources),
                 key=lambda a: int(a.split("_")[1]))
    _atomic_write(root / "state" / DISABLED_FILE,
                  {"disabled": off, "sources": list(sources), "updated_at": int(time.time())})
    return off


def set_sources(root: Path, sources: list[str]) -> tuple[list[str], list[str]]:
    """Записать переключатель и пересобрать паузы. -> (источники, поставленные на паузу аккаунты)."""
    sources = parse_sources(sources)
    _atomic_write(root / "state" / STATE_FILE, {"sources": sources, "updated_at": int(time.time())})
    return sources, sync_disabled(root, sources)


def clear_override(root: Path) -> list[str]:
    try:
        (root / "state" / STATE_FILE).unlink()
    except OSError:
        pass
    src = active_sources(root)
    sync_disabled(root, src)
    return src


def next_mode(sources: list[str]) -> list[str]:
    """Цикл для клавиши панели: оба -> только VLESS -> только прокси -> оба."""
    cur = tuple(parse_sources(sources))
    order = [ALL, ("vless",), ("proxy",)]
    return list(order[(order.index(cur) + 1) % len(order)])


def label(sources: list[str]) -> str:
    s = parse_sources(sources)
    return "VLESS+прокси" if len(s) == 2 else ("только VLESS" if s == ["vless"] else "только прокси")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", dest="mode", help="vless | proxy | all")
    ap.add_argument("--clear", action="store_true", help="снять переключатель панели (брать из config.yaml)")
    args = ap.parse_args()
    if args.mode:
        src, off = set_sources(ROOT, args.mode)
        print(f"Источники: {label(src)}; на паузе из-за источника: {len(off)}")
    elif args.clear:
        print(f"Переключатель снят; источники: {label(clear_override(ROOT))}")
    else:
        rt = runtime_sources(ROOT / "state")
        print(f"Активно: {label(active_sources(ROOT))} (config: {label(config_sources())}; "
              f"переключатель панели: {label(rt) if rt is not None else 'нет'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
