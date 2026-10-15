#!/usr/bin/env python3
"""
scripts/filter_nodes.py — какие ноды пула Reddit ещё пускает (главная страница = HTTP 200).

    venv/bin/python3 scripts/filter_nodes.py                 # отчёт по всему all_nodes.txt
    venv/bin/python3 scripts/filter_nodes.py --write         # + all_nodes.reddit_ok.txt (только годные)

Проверка идёт через delay-API mihomo с expected=200 на https://www.reddit.com/ — через
каждую ноду, без отдельных браузеров. Ноды должны быть загружены в mihomo
(generate_mihomo_config.py + restart). Нода «жива, но Reddit отвечает 403» считается негодной.
"""
import argparse
import collections
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import add_guest_slots as ags  # noqa: E402
import rotation as rot  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes-file", default=None)
    ap.add_argument("--write", action="store_true", help="записать годные ноды в all_nodes.reddit_ok.txt")
    ap.add_argument("--url", default=ags.REDDIT_PROBE_URL)
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    ropts = ags.load_rotation_cfg()
    nf = Path(args.nodes_file or ropts["nodes_file"])
    nf = nf if nf.is_absolute() else ags.ROOT / nf
    if not nf.exists():
        sys.exit(f"нет пула нод {nf}")
    pool = rot.load_pool(nf)
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
    by = collections.Counter(f.country for f in ok)
    tot = collections.Counter(f.country for f in nodes)
    for cc, n in sorted(tot.items(), key=lambda kv: -kv[1]):
        print(f"  {cc}: годных {by.get(cc, 0)} из {n}")
    if len(ok) == len(nodes) and nodes:
        print("\nВНИМАНИЕ: прошли ВСЕ ноды — возможно, твой mihomo не поддерживает параметр expected "
              "(нужен mihomo Meta); проверь вручную: curl -x http://127.0.0.1:ПОРТ https://www.reddit.com/")
    if args.write:
        out = nf.with_name(nf.stem + ".reddit_ok.txt")
        out.write_text("\n".join(f.node.uri for f in ok) + "\n", encoding="utf-8")
        print(f"\nЗаписано {len(ok)} нод -> {out}")
    elif ok:
        print("\nДобавь --write, чтобы сохранить годные ноды в файл.")


if __name__ == "__main__":
    main()
