#!/usr/bin/env python3
"""
scripts/fetch_free_proxies.py — бесплатные прокси (ProxyScrape и любой список `protocol://ip:port`) -> пул ротации.

Что делает (по умолчанию — ТОЛЬКО скачивает, проверяет и печатает итог; запись — с --apply):

  1. Скачивает список (по умолчанию proxyscrape v4, формат protocolipport: по прокси на строку,
     `http://1.2.3.4:80`, `socks4://…`, `socks5://…`) или читает локальный файл (--file).
  2. Разбирает протоколы: http, https (как HTTP-прокси с CONNECT) и socks5 берутся; socks4 пропускается —
     mihomo не умеет исходящий socks4. Дубли и мусор отбрасываются.
  3. Проверка доступности (напрямую, мимо mihomo, много потоков): через прокси открывается
     https://www.cloudflare.com/cdn-cgi/trace (туннель и TLS целы; подмена сертификата = брак, проверка
     сертификатов НЕ отключается), затем https://www.reddit.com/ — ждём HTTP 200, как filter_nodes.py.
     Ответ 403/429 и прочее = Reddit этот IP не пускает, прокси не берётся (--lenient: брать любой ответ < 500).
  4. Лучшие --max прокси (самые быстрые) пишутся в all_proxys.free.txt рядом с all_nodes.txt (бэкап не нужен:
     файл целиком генерируемый; имена free-<ip>-<порт>, комментарий-шапка). Остальные источники не трогаются.

Файл автоматически входит в общий пул (rotation.load_pools): mihomo (generate_mihomo_config.py), filter_nodes.py
и add_login_slots.py/демон видят эти прокси как источник «proxy». Дальше:

    venv/bin/python3 scripts/fetch_free_proxies.py                      # проверить и показать итог
    venv/bin/python3 scripts/fetch_free_proxies.py --apply              # записать all_proxys.free.txt
    venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo
    venv/bin/python3 scripts/filter_nodes.py --write                    # окончательная проверка Reddit через mihomo

Убрать бесплатные прокси из пула: удалить all_proxys.free.txt (и перегенерировать конфиг mihomo).
Бесплатные прокси недолговечны: чаще перезапускай скрипт. Не ставь на них логин-аккаунты с ценными сессиями.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import rotation as rot  # noqa: E402

DEFAULT_URL = ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
               "&proxy_format=protocolipport&format=text")
NEUTRAL_URL = "https://www.cloudflare.com/cdn-cgi/trace"
REDDIT_URL = "https://www.reddit.com/"
LINE_RE = re.compile(r"^(?:(?P<scheme>[A-Za-z0-9]+)://)?(?P<host>[A-Za-z0-9.\-]+):(?P<port>\d{1,5})$")
KEEP = {"http": "http", "https": "http", "socks5": "socks5"}      # https-списки — это HTTP-прокси с CONNECT


def parse_list(text: str) -> tuple[list[str], dict]:
    """Текст списка -> (канонические URI `scheme://host:port`, статистика). Без схемы считается http."""
    stats = {"lines": 0, "kept": 0, "socks4": 0, "other": 0, "junk": 0, "dups": 0}
    out, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        stats["lines"] += 1
        m = LINE_RE.match(line)
        if not m:
            stats["junk"] += 1
            continue
        scheme = (m.group("scheme") or "http").lower()
        host, port = m.group("host"), int(m.group("port"))
        if not (0 < port < 65536):
            stats["junk"] += 1
            continue
        if scheme in ("socks4", "socks4a"):
            stats["socks4"] += 1
            continue
        if scheme not in KEEP:
            stats["other"] += 1
            continue
        uri = f"{KEEP[scheme]}://{host}:{port}"
        if uri in seen:
            stats["dups"] += 1
            continue
        seen.add(uri)
        out.append(uri)
        stats["kept"] += 1
    return out, stats


def download(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 — URL задаёт оператор
        return r.read().decode("utf-8", "ignore")


def _proxies(uri: str) -> dict:
    # socks5h: имя хоста резолвит сам прокси (как это делает mihomo)
    u = uri.replace("socks5://", "socks5h://", 1)
    return {"http": u, "https": u}


def _http_get(url: str, uri: str, timeout: float, impersonate: str = "chrome131") -> int:
    from curl_cffi import requests as cr
    return cr.get(url, proxies=_proxies(uri), impersonate=impersonate, timeout=timeout,
                  allow_redirects=False).status_code


def check_proxy(uri: str, timeout: float = 8.0, lenient: bool = False, get=_http_get) -> dict:
    """-> {"uri", "ok", "ms", "stage", "status"}. stage: neutral | reddit | ok. get(url, uri, timeout) -> status."""
    res = {"uri": uri, "ok": False, "ms": None, "stage": "neutral", "status": None}
    t0 = time.monotonic()
    try:
        st = get(NEUTRAL_URL, uri, timeout)
    except Exception:  # noqa: BLE001 — любая сетевая ошибка = недоступен
        return res
    res["status"] = st
    if st != 200:
        return res
    res["stage"] = "reddit"
    try:
        st = get(REDDIT_URL, uri, timeout)
    except Exception:  # noqa: BLE001
        return res
    res["status"], res["ms"] = st, int((time.monotonic() - t0) * 1000)
    if st == 200 or (lenient and 0 < st < 500):
        res.update(ok=True, stage="ok")
    return res


def check_all(uris: list[str], workers: int = 200, timeout: float = 8.0, lenient: bool = False,
              get=_http_get, progress=None) -> list[dict]:
    out = []
    with cf.ThreadPoolExecutor(max(1, workers)) as ex:
        futs = [ex.submit(check_proxy, u, timeout, lenient, get) for u in uris]
        for i, f in enumerate(cf.as_completed(futs), 1):
            out.append(f.result())
            if progress and i % 200 == 0:
                progress(i, len(uris))
    return out


def proxy_label(uri: str) -> str:
    m = LINE_RE.match(uri)
    return f"free-{m.group('host')}-{m.group('port')}" if m else "free"


def render(good: list[dict]) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    head = (f"# сгенерировано scripts/fetch_free_proxies.py {stamp} UTC — файл перезаписывается целиком\n"
            f"# бесплатные прокси, прошедшие проверку Reddit: {len(good)}\n")
    return head + "".join(f"{r['uri']}#{proxy_label(r['uri'])}\n" for r in good)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="адрес списка (по умолчанию ProxyScrape protocolipport)")
    ap.add_argument("--file", help="взять список из локального файла вместо скачивания")
    ap.add_argument("--workers", type=int, default=200, help="потоков проверки (по умолчанию 200)")
    ap.add_argument("--timeout", type=float, default=8.0, help="таймаут одного запроса, с")
    ap.add_argument("--max", type=int, default=300, dest="max_keep",
                    help="сколько лучших прокси оставить (каждый узел попадает во ВСЕ группы mihomo)")
    ap.add_argument("--limit", type=int, default=0, help="проверить только первые N (для пробы)")
    ap.add_argument("--lenient", action="store_true", help="брать любой ответ Reddit < 500 (403 тоже), не только 200")
    ap.add_argument("--apply", action="store_true", help="записать all_proxys.free.txt (без флага — только отчёт)")
    ap.add_argument("--nodes-file", default=None, help="пул VLESS (рядом с ним лежит all_proxys.free.txt)")
    args = ap.parse_args()

    try:
        text = Path(args.file).read_text(encoding="utf-8", errors="ignore") if args.file else download(args.url)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА: не получил список: {e}")
        return 2
    uris, st = parse_list(text)
    print(f"Список: строк {st['lines']}, принято {st['kept']} (http/https/socks5), "
          f"socks4 пропущено {st['socks4']}, другие протоколы {st['other']}, мусор {st['junk']}, дублей {st['dups']}")
    if not uris:
        print("Нечего проверять.")
        return 1
    if args.limit:
        uris = uris[:args.limit]
    print(f"Проверяю {len(uris)} прокси ({args.workers} потоков, таймаут {args.timeout:.0f} с)...")
    t0 = time.monotonic()
    results = check_all(uris, args.workers, args.timeout, args.lenient,
                        progress=lambda i, n: print(f"  {i}/{n}"))
    good = sorted((r for r in results if r["ok"]), key=lambda r: r["ms"])
    stages = {"neutral": 0, "reddit": 0}
    for r in results:
        if not r["ok"]:
            stages[r["stage"]] += 1
    print(f"\nГотово за {time.monotonic() - t0:.0f} с: годных {len(good)} из {len(results)}; "
          f"не открыли туннель {stages['neutral']}, открыли, но Reddit не пустил {stages['reddit']}")
    if good:
        print("Самые быстрые: " + ", ".join(f"{proxy_label(r['uri'])} {r['ms']}мс" for r in good[:5]))
    keep = good[:max(0, args.max_keep)]
    if len(keep) < len(good):
        print(f"В файл пойдут {len(keep)} самых быстрых (--max {args.max_keep}).")
    if not args.apply:
        print("\n[отчёт] ничего не записано. Для записи добавь --apply")
        return 0
    if not keep:
        print("\nГодных нет — all_proxys.free.txt НЕ перезаписываю.")
        return 1
    nf = Path(args.nodes_file or rot.DEFAULT_OPTS["nodes_file"])
    nf = nf if nf.is_absolute() else rot.ROOT / nf
    path = rot.free_proxies_path(nf)
    write_atomic(path, render(keep))
    print(f"\nOK: {len(keep)} прокси записано в {path.name}")
    print("Дальше: generate_mihomo_config.py && docker compose restart mihomo; затем filter_nodes.py --write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
