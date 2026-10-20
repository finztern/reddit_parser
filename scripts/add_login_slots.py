#!/usr/bin/env python3
"""
scripts/add_login_slots.py — добирает ЛОГИН-слоты до числа годных нод (модуль 01).

Число слотов = число ГОДНЫХ нод (Reddit отдаёт 200) минус запас (--spare, по
умолчанию fleet.reserve_nodes = 6). Годные ноды — это all_nodes.reddit_ok.txt,
его пишет `filter_nodes.py --write` (или --probe: проверить пул прямо здесь).

Что делает (по умолчанию — ТОЛЬКО печатает план; запись — с --apply):

  1. Считает цель: годных нод - spare. Уже назначенные ноды (vless_accounts.env)
     входят в «годные», поэтому новых слотов = цель - слотов с нодой.
  2. Сначала выдаёт ноды СУЩЕСТВУЮЩИМ логин-слотам без ноды (например, после
     convert_to_login.py), потом создаёт новые account_N подряд после
     последнего. Порты — с 7946 и выше, свободные и без дублей.
  3. Ноды — только свободные: не назначены, нет в реестре демона
     (state/identities.json), не плохие; страны чередуются (round-robin).
  4. Пишет (бэкапы *.bak-addlogin-<время>):
       vless_accounts.env              account_N = http://user:pass@host:port
       accounts.yaml                   enabled: false (до входа), login: true, cookie_file,
                                       storage_state, порт, locale, отпечаток
       scripts/refresh_cookies.yaml    login_accounts += N, account_geo[N] под ноду
     Гостевые файлы и rotation.slots НЕ трогаются.
  5. Самопроверка: нет дублей порта/ноды, всё согласовано.

Новые слоты не стартуют, пока в них не войдёшь: manual_login.py account_N.

Запуск (из корня проекта, тем же python, где стоит curl_cffi):

    venv/bin/python3 scripts/filter_nodes.py --write         # годные ноды -> all_nodes.reddit_ok.txt
    venv/bin/python3 scripts/add_login_slots.py              # план
    venv/bin/python3 scripts/add_login_slots.py --apply      # запись
"""

from __future__ import annotations

import argparse
import json
import random
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


def allocate_login_ports(used: set[int], n: int, check=ags.port_free, first: int = lf.FIRST_LOGIN_PORT) -> list[int]:
    """n свободных портов, начиная с max(first, max(used)+1)."""
    out, port = [], max(first, max(used, default=first - 1) + 1)
    while len(out) < n:
        if port not in used and check(port):
            out.append(port)
        port += 1
    return out


def good_keys_from_file(path: Path, sources: list[str] | None = None) -> set[str]:
    pool = rot.load_pool(path)
    return {n.key for n in (rot.of_sources(pool, sources) if sources is not None else pool)}


def good_ranks_from_file(path: Path, sources: list[str] | None = None) -> dict[str, int]:
    """key -> место в файле (filter_nodes.py пишет самые быстрые первыми); только включённые источники."""
    pool = rot.load_pool(path)
    if sources is not None:
        pool = rot.of_sources(pool, sources)
    return {n.key: i for i, n in enumerate(pool)}


def plan_counts(good: int, spare: int, have: int, missing: int, free: int) -> dict:
    """Сколько слотов выдать. target = good - spare; budget = target - have;
    выдать можно не больше свободных нод сверх запаса (free - spare)."""
    target = max(0, good - max(0, spare))
    budget = max(0, target - have)
    take = min(budget, max(0, free - max(0, spare)))
    n_missing = min(missing, take)
    return {"target": target, "budget": budget, "take": take, "n_missing": n_missing, "n_new": take - n_missing}


def run(p: lf.Paths, spare: int | None, apply: bool, probe: bool, nodes_file: str | None, seed: int | None,
        probe_timeout_ms: int = 8000, probe_workers: int = 8, out=print) -> int:
    rng = random.Random(seed)
    fleet = lf.load_fleet_cfg(p.config)
    spare = fleet["reserve_nodes"] if spare is None else spare
    ropts = {**rot.DEFAULT_OPTS, **((lf.load_yaml(p.config).get("rotation")) or {})}

    accounts_text = p.accounts.read_text(encoding="utf-8")
    login_text = p.login.read_text(encoding="utf-8")
    accounts = (yaml.safe_load(accounts_text) or {}).get("accounts") or []
    login_cfg = yaml.safe_load(login_text) or {}
    by_name = {a["name"]: a for a in accounts}
    login_names = lf.login_account_names(login_cfg, accounts)
    links = lf.load_links(p)
    try:
        ident = json.loads(p.ident.read_text(encoding="utf-8")) if p.ident.exists() else {}
    except (OSError, ValueError):
        ident = {}

    nf = Path(nodes_file or ropts["nodes_file"])
    nf = nf if nf.is_absolute() else p.root / nf
    if not rot.pools_exist(nf, ropts.get("proxies_file")):
        out(f"ОШИБКА: нет пула нод {nf} (и {rot.proxies_path(nf, ropts.get('proxies_file')).name})")
        return 2
    sources = rot.active_sources(p.root)
    out(f"Источники узлов: {', '.join(sources)}")
    ok_file = nf.with_name(nf.stem + ".reddit_ok.txt")

    # --- годные ноды ---
    pool = rot.load_pools(nf, ropts.get("proxies_file"))
    if probe:
        names = rot.pool_names(pool, set(), set())
        nodes = [ags.Free(n, names[n.key], ags.country_of(names[n.key]))
                 for n in rot.of_sources(pool, sources) if names.get(n.key)]
        try:
            ok, bad, absent = ags.probe_nodes(nodes, ropts["mihomo_api"], ropts.get("mihomo_secret", ""),
                                              ags.REDDIT_PROBE_URL, probe_timeout_ms, probe_workers, "200")
        except rot.RotationError as e:
            out(f"ОШИБКА: {e}\nЗапусти mihomo (docker compose up -d mihomo).")
            return 2
        good = {f.node.key for f in ok}
        ranks = {}
        out(f"Проверка Reddit через mihomo: годных {len(ok)}, не пускает {len(bad)}, нет в mihomo {len(absent)}")
    else:
        if not ok_file.exists():
            out(f"ОШИБКА: нет {ok_file.name} — сначала `scripts/filter_nodes.py --write` (или запусти с --probe).")
            return 2
        ranks = good_ranks_from_file(ok_file, sources)
        good = set(ranks)
        age_h = (time.time() - ok_file.stat().st_mtime) / 3600
        out(f"Годных нод (из {ok_file.name}, возраст {age_h:.1f}ч): {len(good)}")
        if age_h > 2 * fleet["node_check_interval_hours"]:
            out(f"  ВНИМАНИЕ: файл старше {2 * fleet['node_check_interval_hours']}ч — перезапусти filter_nodes.py --write")
    if len(good) < fleet["min_good_nodes_warn"]:
        out(f"ВНИМАНИЕ: годных нод {len(good)} < fleet.min_good_nodes_warn={fleet['min_good_nodes_warn']}")

    # --- свободные годные ноды ---
    # имена в mihomo строятся по ПОЛНОМУ пулу (как generate_mihomo_config), а не по подмножеству годных
    free_all = ags.find_free_nodes(nf, links, ident, sources, ropts.get("proxies_file"))
    free = [f for f in free_all if f.node.key in good]
    for f in free:
        f.rank = ranks.get(f.node.key)
    if probe:   # свежие задержки из probe_nodes лежат в ok-узлах
        by_key = {f.node.key: f.delay for f in ok}
        for f in free:
            f.delay = by_key.get(f.node.key)

    have = sum(1 for a in accounts if a["name"] in links and a["name"] in login_names)
    missing = sorted((n for n in login_names if n in by_name and n not in links), key=lf.acc_num)
    c = plan_counts(len(good), spare, have, len(missing), len(free))
    out(f"Слотов с нодой: {have}; без ноды: {len(missing)}; свободных годных нод: {len(free)}; запас: {spare}")
    out(f"Цель: годные {len(good)} - запас {spare} = {c['target']} слотов; нужно добавить {c['budget']}; выдаю {c['take']}")
    if c["take"] < c["budget"]:
        out(f"ВНИМАНИЕ: не хватает свободных нод (нужно {c['budget']}, после запаса доступно {max(0, len(free) - spare)})")
    if c["take"] == 0:
        out("Изменений нет — выхожу.")
        return 0

    chosen = ags.pick_nodes(free, c["take"], rng)
    unknown_cc = sorted({f.country for f in chosen if f.country not in rot.COUNTRY_GEO})
    if unknown_cc:
        out(f"ВНИМАНИЕ: страны {', '.join(unknown_cc)} нет в COUNTRY_GEO (rotation.py) — гео будет en-US/New_York.")

    n_missing, n_new = c["n_missing"], len(chosen) - c["n_missing"]
    new_start = max((lf.acc_num(a["name"]) for a in accounts), default=0) + 1
    new_slots = [f"account_{new_start + i}" for i in range(n_new)]
    used_ports = {int(a["proxy_port"]) for a in accounts if a.get("proxy_port")}
    ports = allocate_login_ports(used_ports, n_new)

    used_imps = {a.get("impersonate") for a in accounts}
    fp_args = SimpleNamespace(allow_old=False, no_edge=True, no_mobile=False)
    profiles, reused = ags.pick_profiles(n_new, used_imps, rng, fp_args) if n_new else ([], False)
    if reused:
        out("ВНИМАНИЕ: свободных impersonate-таргетов меньше, чем новых слотов — часть повторится.")

    taken_names = set()
    for acc, link in links.items():
        try:
            taken_names.add(gmc.parse_node(link, acc)["name"])
        except ValueError:
            taken_names.add(acc)

    rows = []   # (slot, node, uri, pname, geo, is_new, port, profile)
    for i, f in enumerate(chosen):
        uri, pname = ags.unique_link(f.node.uri, taken_names)
        geo = rot.geo_for(f.mihomo_name)
        if i < n_missing:
            rows.append((missing[i], f, uri, pname, geo, False, by_name[missing[i]].get("proxy_port"), None))
        else:
            j = i - n_missing
            rows.append((new_slots[j], f, uri, pname, geo, True, ports[j], profiles[j]))

    out(f"\n{'слот':<11} {'порт':<5} {'нода (mihomo)':<24} {'гео':<7} {'impersonate':<18} примечание")
    for slot, f, uri, pname, geo, is_new, port, prof in rows:
        imp = prof.impersonate if prof else by_name[slot].get("impersonate")
        out(f"{slot:<11} {port!s:<5} {pname[:24]:<24} {geo['locale']:<7} {imp!s:<18} "
            f"{'НОВЫЙ слот' if is_new else 'нода для существующего слота'}")
    out(f"\nСвободных годных нод останется про запас: {len(free) - len(chosen)}")

    # --- новые тексты ---
    new_accounts = accounts_text
    for slot, f, uri, pname, geo, is_new, port, prof in rows:
        if not is_new:
            new_accounts = ags.patch_account_fields(new_accounts, slot, locale=geo["locale"])
    added = [r for r in rows if r[5]]
    if added:
        stamp0 = time.strftime("%Y%m%d-%H%M%S")
        new_accounts = (new_accounts.rstrip("\n")
                        + f"\n\n  # --- добавлено scripts/add_login_slots.py {stamp0} (логин-слоты, enabled: false до входа) ---\n\n"
                        + "\n".join(lf.login_account_block(r[0], r[6], r[4]["locale"], r[7].impersonate, r[7].user_agent)
                                    for r in added))
    geo_by = {r[0]: r[4] for r in rows}
    labels = {r[0]: r[3] for r in rows}
    new_login = lf.patch_login_yaml(login_text, [r[0] for r in rows], geo_by, labels,
                                    overwrite_geo={r[0] for r in rows if not r[5]})

    links2 = dict(links)
    links2.update({r[0]: r[2] for r in rows})
    acc2 = (yaml.safe_load(new_accounts) or {}).get("accounts") or []
    problems = lf.validate_fleet(acc2, links2, None, yaml.safe_load(new_login) or {})
    if problems:
        out("ОШИБКА: результат не согласован — ничего не записываю:\n  " + "\n  ".join(problems))
        return 1

    if not apply:
        out("\n[план] ничего не записано. Для записи добавь --apply")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (p.env, p.accounts, p.login):
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-addlogin-{stamp}"))
    env_text = p.env.read_text(encoding="utf-8") if p.env.exists() else ""
    env_text = env_text.rstrip("\n") + f"\n\n# --- добавлено scripts/add_login_slots.py {stamp} ---\n"
    env_text += "".join(f"{r[0]} = {r[2]}\n" for r in rows)
    p.env.write_text(env_text, encoding="utf-8")
    with open(p.accounts, "r+", encoding="utf-8") as f:     # тот же inode (файл смонтирован в контейнер)
        f.seek(0)
        f.write(new_accounts)
        f.truncate()
    p.login.write_text(new_login, encoding="utf-8")

    rot.sync_source_pauses(p.root)   # аккаунты узлов выключенного источника — на паузу
    out(f"\nOK: записано (бэкапы *.bak-addlogin-{stamp}): vless_accounts.env, accounts.yaml, scripts/refresh_cookies.yaml")
    out("\nДальше (из корня проекта):")
    out("  1) venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo")
    out("  2) sudo systemctl restart rotation-daemon")
    out("  3) войти в слоты: venv/bin/python3 scripts/manual_login.py account_N   (список «нужен вход»: --list)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spare", type=int, default=None, help="запас годных нод (по умолчанию fleet.reserve_nodes)")
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только план)")
    ap.add_argument("--probe", action="store_true", help="не читать all_nodes.reddit_ok.txt, а проверить пул через mihomo")
    ap.add_argument("--nodes-file", default=None, help="пул нод (по умолчанию rotation.nodes_file из config.yaml)")
    ap.add_argument("--probe-timeout-ms", type=int, default=8000)
    ap.add_argument("--probe-workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--root", default=str(lf.ROOT), help="каталог проекта (по умолчанию — этот репозиторий)")
    args = ap.parse_args()
    return run(lf.Paths(Path(args.root).resolve()), args.spare, args.apply, args.probe, args.nodes_file, args.seed,
               args.probe_timeout_ms, args.probe_workers)


if __name__ == "__main__":
    sys.exit(main())
