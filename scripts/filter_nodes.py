#!/usr/bin/env python3
"""
scripts/filter_nodes.py — какие ноды пула Reddit ещё пускает (главная страница = HTTP 200).

    venv/bin/python3 scripts/filter_nodes.py                 # отчёт по всему all_nodes.txt
    venv/bin/python3 scripts/filter_nodes.py --write         # + all_nodes.reddit_ok.txt (только годные)

Раз в сутки запускается таймером filter-nodes.timer (fleet.node_check_interval_hours): годные ноды
обновляют all_nodes.reddit_ok.txt — из него берут ноды add_login_slots.py и демон ротации (замена
сгоревшей ноды логин-слота), — а отчёт пишется в state/node_report.json (виден в
`rotation_daemon.py --status`). Пустой результат (mihomo лёг / Reddit не пускает никого) файл годных
нод НЕ затирает.

Проверяются узлы обоих источников — VLESS (all_nodes.txt) и прокси (all_proxys.txt); годные пишутся в
all_nodes.reddit_ok.txt вперемешку, самые быстрые первыми. Какие источники в работе, решает
scripts/node_sources.py (config rotation.node_sources / клавиша `n` в панели).

Проверка идёт через delay-API mihomo с expected=200 на https://www.reddit.com/ — через
каждую ноду, без отдельных браузеров. Ноды должны быть загружены в mihomo
(generate_mihomo_config.py + restart). Нода «жива, но Reddit отвечает 403» считается негодной.
"""
import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import add_guest_slots as ags  # noqa: E402
import login_fleet as lf  # noqa: E402
import rotation as rot  # noqa: E402


def build_report(total: int, good: int, bad: int, absent: int, by_country: dict, fleet: dict) -> dict:
    """Отчёт «в панель»: сколько нод годных, хватает ли на слоты с запасом."""
    return {
        "ts": time.time(),
        "total": total, "good": good, "bad": bad, "absent": absent,
        "by_country": by_country,                      # {cc: [годных, всего]}
        "reserve_nodes": fleet["reserve_nodes"],
        "slots_target": max(0, good - fleet["reserve_nodes"]),
        "min_good_nodes_warn": fleet["min_good_nodes_warn"],
        "warn": good < fleet["min_good_nodes_warn"],
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o777)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o666)
    except OSError:
        pass
    os.replace(tmp, path)


def proxy_url_of(uri: str) -> str:
    return uri.split("#", 1)[0]


def resolve_countries(ok: list, workers: int, lookup=None) -> int:
    """Страна по реальному выходному IP (ipinfo/ip-api ЧЕРЕЗ сам прокси) для годных
    прокси без кода страны в имени -> state/proxy_geo.json {имя: "US"}. -> сколько определено.
    Гео слота (locale/timezone) должно совпадать со страной выходного IP."""
    from concurrent.futures import ThreadPoolExecutor
    if lookup is None:
        sys.path.insert(0, str(ags.ROOT))
        from scraper import geoip
        lookup = geoip.lookup_exit_ip
    todo = [f for f in ok if f.country == "??" and ags.gmc.link_kind(f.node.uri) == "proxy"]   # у VLESS страна в имени
    path = ags.ROOT / "state" / "proxy_geo.json"
    cache = ags.read_json(path)
    todo = [f for f in todo if not str(cache.get(f.mihomo_name, "")).strip()]

    def one(f):
        try:
            return f, lookup(proxy_url_of(f.node.uri), timeout=10.0)["country"]
        except Exception:
            return f, None

    n = 0
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for f, cc in ex.map(one, todo):
            if cc and len(str(cc)) == 2:
                cache[f.mihomo_name] = str(cc).upper()
                n += 1
    if n:
        write_report(path, cache)
    for f in ok:
        f.country = rot.country_for(f.mihomo_name)
    print(f"Страна определена по выходному IP: {n} из {len(todo)} прокси")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes-file", default=None)
    ap.add_argument("--write", action="store_true", help="записать годные ноды в all_nodes.reddit_ok.txt")
    ap.add_argument("--url", default=ags.REDDIT_PROBE_URL)
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-geo", action="store_true",
                    help="не определять страну прокси по выходному IP (гео слотов тогда en-US, если в имени нет кода страны)")
    ap.add_argument("--geo-workers", type=int, default=8)
    args = ap.parse_args()

    ropts = ags.load_rotation_cfg()
    nf = Path(args.nodes_file or ropts["nodes_file"])
    nf = nf if nf.is_absolute() else ags.ROOT / nf
    if not rot.pools_exist(nf, ropts.get("proxies_file")):
        sys.exit(f"нет пула нод {nf} (и {rot.proxies_path(nf, ropts.get('proxies_file')).name})")
    pool = rot.load_pools(nf, ropts.get("proxies_file"))   # проверяем узлы ВСЕХ источников: в mihomo они загружены всегда
    names = rot.pool_names(pool, set(), set())
    nodes = [ags.Free(n, names[n.key], ags.country_of(names[n.key])) for n in pool if names.get(n.key)]
    print(f"Нод в пуле: {len(nodes)}; проверяю {args.url} через каждую (ожидаю 200)...")
    try:
        ok, bad, absent = ags.probe_nodes(nodes, ropts["mihomo_api"], ropts.get("mihomo_secret", ""),
                                          args.url, args.timeout_ms, args.workers, "200")
    except rot.RotationError as e:
        sys.exit(f"ОШИБКА: {e}\nЗапусти mihomo (docker compose up -d mihomo).")

    print(f"\nReddit пускает: {len(ok)}   не пускает/не отвечает: {len(bad)}   нет в mihomo: {len(absent)}")
    if absent and not ok:
        sys.exit("Пул не загружен в mihomo: generate_mihomo_config.py && docker compose restart mihomo")
    ok.sort(key=lambda f: f.delay if f.delay is not None else float("inf"))   # быстрые первыми
    if ok:
        print(f"Самые быстрые: {', '.join(f'{f.mihomo_name} {f.delay}мс' for f in ok[:5])}")
    if not args.no_geo and ok:
        resolve_countries(ok, args.geo_workers)
    by = collections.Counter(f.country for f in ok)
    tot = collections.Counter(f.country for f in nodes)
    for cc, n in sorted(tot.items(), key=lambda kv: -kv[1]):
        print(f"  {cc}: годных {by.get(cc, 0)} из {n}")
    if len(ok) == len(nodes) and nodes:
        print("\nВНИМАНИЕ: прошли ВСЕ ноды — возможно, твой mihomo не поддерживает параметр expected "
              "(нужен mihomo Meta); проверь вручную: curl -x http://127.0.0.1:ПОРТ https://www.reddit.com/")
    fleet = lf.load_fleet_cfg(ags.CONFIG_PATH)
    report = build_report(len(nodes), len(ok), len(bad), len(absent),
                          {cc: [by.get(cc, 0), n] for cc, n in tot.items()}, fleet)
    write_report(ags.ROOT / "state" / "node_report.json", report)
    print(f"\nОтчёт: state/node_report.json — годных {report['good']}, слотов можно держать "
          f"{report['slots_target']} (запас {report['reserve_nodes']})")
    if report["warn"]:
        print(f"ВНИМАНИЕ: годных нод {report['good']} < fleet.min_good_nodes_warn={report['min_good_nodes_warn']} — "
              "пополни all_nodes.txt / all_proxys.txt")

    if args.write:
        if not ok:
            sys.exit("Ни одной годной ноды — all_nodes.reddit_ok.txt НЕ перезаписываю (проверь mihomo/Reddit).")
        out = nf.with_name(nf.stem + ".reddit_ok.txt")
        out.write_text("\n".join(f.node.uri for f in ok) + "\n", encoding="utf-8")
        print(f"\nЗаписано {len(ok)} нод -> {out}")
    elif ok:
        print("\nДобавь --write, чтобы сохранить годные ноды в файл.")


if __name__ == "__main__":
    main()
