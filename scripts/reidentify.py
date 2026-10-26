#!/usr/bin/env python3
"""
scripts/reidentify.py — замена НОДЫ и ПРОФИЛЯ (impersonate / user-agent / locale) слота, на котором не идёт
регистрация (например, Reddit не отправляет код подтверждения с этой ноды).

Что делает (по умолчанию — ТОЛЬКО план; запись — с --apply):

  1. Берёт свободную ГОДНУЮ ноду (all_nodes.reddit_ok.txt, не назначена, не в bad, включённый источник),
     самую быструю первой; переключает на неё группу pg-<слот> в mihomo и проверяет живость (precheck).
     Мёртвые кандидаты пропускаются; если живой не нашлось — файлы НЕ меняются.
  2. Подбирает новый профиль: другой impersonate-таргет + user-agent (rotate_fingerprints.build_profile).
  3. Пишет (бэкапы *.bak-reident-<время>): vless_accounts.env (нода слота), accounts.yaml (impersonate,
     user_agent, locale), scripts/refresh_cookies.yaml (account_geo). Старую ноду помечает «плохой»
     (state/rotation_state.json -> bad), чтобы её не выдали другому слоту.
  4. Профиль устройства state/profiles/<слот>.json (если есть) переименовывается в *.replaced-<время>:
     иначе он перебил бы новые UA/locale.

Только для слота, в который ещё НЕ входили (регистрация): сессия/cookies не переносятся. Вызывается
автоматически из login_hub.py / manual_login.py --register после 3 неудачных попыток отправки кода.
Если запущен rotation-daemon, после замены перезапусти его (иначе он вернёт старую ноду в mihomo по реестру).

    venv/bin/python3 scripts/reidentify.py account_96            # план
    venv/bin/python3 scripts/reidentify.py account_96 --apply    # запись
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import add_guest_slots as ags  # noqa: E402
import generate_mihomo_config as gmc  # noqa: E402
import login_fleet as lf  # noqa: E402
import rotation as rot  # noqa: E402

MAX_PROBE = 5          # сколько кандидатов-нод проверить на живость, прежде чем сдаться


def _yq(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def replace_env_line(text: str, account: str, uri: str) -> str:
    pat = re.compile(rf"(?m)^(\s*){re.escape(account)}\s*=.*$")
    if pat.search(text):
        return pat.sub(lambda m: f"{m.group(1)}{account} = {uri}", text, count=1)
    return text.rstrip("\n") + f"\n{account} = {uri}\n"


def mark_bad_default(key: str, name: str, account: str, reason: str) -> None:
    try:
        st = rot.load_state()
        st["bad"][key] = {"ts": time.time(), "name": name, "account": account, "reason": reason}
        rot.save_state(st)
    except Exception:  # noqa: BLE001 — пометка не критична
        pass


def swap_identity(account: str, *, root: Path | None = None, rng: random.Random | None = None,
                  tried: set[str] | None = None, apply: bool = True, mihomo=None, precheck=rot.precheck,
                  mark_bad=mark_bad_default, reason: str = "register_failed", out=print,
                  sleep=time.sleep) -> dict | None:
    """-> {"node": имя_в_mihomo, "country", "impersonate", "locale"} или None (замены нет / план без записи
    возвращает то же описание). `tried` — ключи нод, уже испробованные в этой сессии (пополняется)."""
    p = lf.Paths(root or rot.ROOT)
    rng = rng or random.Random()
    tried = tried if tried is not None else set()
    accounts_text = p.accounts.read_text(encoding="utf-8")
    accounts = (yaml.safe_load(accounts_text) or {}).get("accounts") or []
    acc = next((a for a in accounts if a.get("name") == account), None)
    if not acc or not acc.get("proxy_port"):
        out(f"[{account}] нет в accounts.yaml / нет proxy_port")
        return None
    port = int(acc["proxy_port"])
    links = lf.load_links(p)
    old_uri = links.get(account)
    try:
        ident = json.loads(p.ident.read_text(encoding="utf-8")) if p.ident.exists() else {}
    except (OSError, ValueError):
        ident = {}
    ropts = {**rot.DEFAULT_OPTS, **((lf.load_yaml(p.config).get("rotation")) or {})}
    nf = Path(ropts["nodes_file"])
    nf = nf if nf.is_absolute() else p.root / nf
    sources = rot.active_sources(p.root)
    ok_file = nf.with_name(nf.stem + ".reddit_ok.txt")
    ranks = {}
    if ok_file.exists():
        pool_ok = rot.load_pool(ok_file)
        pool_ok = rot.of_sources(pool_ok, sources)
        ranks = {n.key: i for i, n in enumerate(pool_ok)}
    free = ags.find_free_nodes(nf, links, ident, sources, ropts.get("proxies_file"))
    if ranks:
        free = [f for f in free if f.node.key in ranks]
        free.sort(key=lambda f: ranks[f.node.key])
    free = [f for f in free if f.node.key not in tried]
    if not free:
        out(f"[{account}] нет свободных годных нод (проверь all_nodes.reddit_ok.txt: filter_nodes.py --write)")
        return None

    mihomo = mihomo or rot.Mihomo(ropts["mihomo_api"], ropts.get("mihomo_secret", ""))
    group = f"pg-{account}"
    chosen = None
    for f in (free[:MAX_PROBE] if apply else free[:1]):
        if not apply:                       # план: mihomo не трогаем, живость не проверяем
            chosen = f
            break
        tried.add(f.node.key)
        try:
            mihomo.select(group, f.mihomo_name)
            mihomo.close_inbound(port)
            sleep(0.5)
            alive, info = precheck(port)
        except rot.RotationError as e:
            out(f"[{account}] mihomo не принял ноду {f.mihomo_name}: {e}")
            continue
        if alive:
            chosen = f
            out(f"[{account}] новая нода {f.mihomo_name} ({f.country}): {info}")
            break
        out(f"[{account}] кандидат {f.mihomo_name} не отвечает ({info}) — пробую следующего")
        mark_bad(f.node.key, f.mihomo_name, account, "precheck")
    if chosen is None:
        out(f"[{account}] живой ноды не нашлось — файлы не менял")
        return None

    taken = set()
    for a, link in links.items():
        if a == account:
            continue
        try:
            taken.add(gmc.parse_node(link, a)["name"])
        except ValueError:
            taken.add(a)
    uri, pname = ags.unique_link(chosen.node.uri, taken)
    geo = rot.geo_for(chosen.mihomo_name)
    used_imps = {a.get("impersonate") for a in accounts if a.get("name") != account}
    profiles, _reused = ags.pick_profiles(1, used_imps, rng, SimpleNamespace(allow_old=False, no_edge=True,
                                                                              no_mobile=False))
    prof = profiles[0]

    new_accounts = accounts_text
    new_accounts = lf.set_account_key(new_accounts, account, "impersonate", prof.impersonate)
    new_accounts = lf.set_account_key(new_accounts, account, "user_agent", _yq(prof.user_agent))
    new_accounts = ags.patch_account_fields(new_accounts, account, locale=geo["locale"])
    login_text = p.login.read_text(encoding="utf-8")
    new_login = lf.patch_login_yaml(login_text, [account], {account: geo}, {account: pname},
                                    overwrite_geo={account})
    links2 = {**links, account: uri}
    problems = lf.validate_fleet((yaml.safe_load(new_accounts) or {}).get("accounts") or [], links2, None,
                                 yaml.safe_load(new_login) or {})
    result = {"node": chosen.mihomo_name, "country": chosen.country, "impersonate": prof.impersonate,
              "locale": geo["locale"]}
    if problems:
        out(f"[{account}] результат не согласован — ничего не записываю:\n  " + "\n  ".join(problems))
        return None
    if not apply:
        out(f"[{account}] [план] нода {chosen.mihomo_name}, impersonate {prof.impersonate}, locale {geo['locale']}; "
            "для записи --apply")
        return result

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (p.env, p.accounts, p.login):
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-reident-{stamp}"))
    env_text = p.env.read_text(encoding="utf-8") if p.env.exists() else ""
    rot._atomic_write(p.env, replace_env_line(env_text, account, uri))
    rot._write_inplace(p.accounts, new_accounts)       # тот же inode (файл смонтирован в контейнер)
    rot._atomic_write(p.login, new_login)
    if old_uri:
        mark_bad(rot.node_key(old_uri), account, account, reason)
    prof_file = p.state / "profiles" / f"{account}.json"
    if prof_file.exists():
        prof_file.rename(prof_file.with_name(f"{prof_file.name}.replaced-{stamp}"))
    out(f"[{account}] записано (бэкапы *.bak-reident-{stamp}): vless_accounts.env, accounts.yaml, refresh_cookies.yaml")
    if p.ident.exists() and account in ((ident.get("slots")) or {}):
        out(f"[{account}] ВНИМАНИЕ: слот есть в реестре rotation-daemon — перезапусти демон, иначе он вернёт старую ноду")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("account")
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только план)")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    res = swap_identity(args.account, root=Path(args.root) if args.root else None, apply=args.apply)
    return 0 if res else 1


if __name__ == "__main__":
    sys.exit(main())
