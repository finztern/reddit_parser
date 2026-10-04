#!/usr/bin/env python3
"""
scripts/test_curl_guest.py

Проверка: можно ли получить гостевые cookies Reddit БЕЗ браузера, тем же
curl_cffi (impersonate + прокси слота), которым потом идут запросы.

Шаги для слота:
  0. baseline: comments.json без cookies (ожидаем 403)
  1. GET / тем же клиентом -> смотрим, какие cookies выставил Reddit
     (ищем loid, session_tracker, reddit_session, csrf_token и т.п.)
  2. GET comments.json с накопленными cookies -> 200 = метод работает
  3. (--write) атомарно пишет cookies/account_N.json (Cookie Editor формат)

Запуск (из корня проекта, где стоит curl_cffi):
    python3 scripts/test_curl_guest.py --account account_5
    python3 scripts/test_curl_guest.py --account account_5 --account account_10
    python3 scripts/test_curl_guest.py --all-enabled
    python3 scripts/test_curl_guest.py --account account_5 --write
    python3 scripts/test_curl_guest.py --account account_5 --no-proxy   # дебаг без mihomo

Exit code 0 — хотя бы... нет: 0 только если ВСЕ проверенные слоты получили 200.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml
from curl_cffi import requests as cffi

ROOT = Path(__file__).resolve().parent.parent
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
BASE = "https://www.reddit.com"
KEY_COOKIES = ("loid", "session_tracker", "reddit_session", "csrf_token", "token_v2", "edgebucket")


def load_accounts() -> list[dict]:
    with open(ROOT / "accounts.yaml", "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("accounts", [])


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def jar_to_rows(sess) -> list[dict]:
    rows = []
    for c in sess.cookies.jar:
        rows.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or ".reddit.com",
            "path": c.path or "/",
            "expirationDate": c.expires if c.expires else -1,
            "httpOnly": False,
            "secure": bool(c.secure),
            "sameSite": "Lax",
        })
    return rows


def snippet(resp, n=150) -> str:
    try:
        return resp.text[:n].replace("\n", " ")
    except Exception:
        return "<нет тела>"


def test_slot(acc: dict, args) -> bool:
    name = acc["name"]
    imp = acc.get("impersonate")
    port = acc.get("proxy_port")
    print(f"\n=== {name}  impersonate={imp}  port={port} ===")
    if not imp:
        print("  нет impersonate в accounts.yaml — пропуск")
        return False

    proxies = None
    if not args.no_proxy:
        if not port:
            print("  нет proxy_port — пропуск")
            return False
        url = f"http://{PROXY_HOST}:{port}"
        proxies = {"http": url, "https": url}

    verify_url = f"{BASE}/r/{args.subreddit}/comments.json"
    kw = dict(impersonate=imp, proxies=proxies, timeout=args.timeout)

    # 0. baseline без cookies
    try:
        r0 = cffi.get(verify_url, params={"limit": 5}, **kw)
        print(f"  [0] baseline без cookies: {r0.status_code}")
    except Exception as e:
        print(f"  [0] baseline упал: {e}  (прокси/mihomo живы?)")
        return False

    # 1. прогрев главной тем же клиентом (Session хранит cookies сама)
    # UA/заголовки НЕ задаём: impersonate ставит родные для этого браузера.
    sess = cffi.Session(**kw)
    paths = ["/"] + [p for p in (args.extra_path or [])]
    for i, p in enumerate(paths):
        try:
            r = sess.get(BASE + p, allow_redirects=True)
        except Exception as e:
            print(f"  [1] GET {p} упал: {e}")
            return False
        print(f"  [1] GET {p}: {r.status_code}, {len(r.content)} байт, "
              f"cookies в jar: {len(list(sess.cookies.jar))}")
        if r.status_code in (403, 429) or "captcha" in r.text[:2000].lower():
            print(f"      тело: {snippet(r)}")
        if i < len(paths) - 1:
            time.sleep(args.pause)

    rows = jar_to_rows(sess)
    names = [c["name"] for c in rows]
    print(f"  cookies: {', '.join(names) if names else '— нет —'}")
    found = [k for k in KEY_COOKIES if k in names]
    print(f"  ключевые найдены: {', '.join(found) if found else '— нет —'}")
    if not rows:
        print("  ИТОГ: Reddit не выдал cookies без браузера -> нужен браузер (метод не работает)")
        return False

    # 2. верификация
    try:
        r2 = sess.get(verify_url, params={"limit": 5})
    except Exception as e:
        print(f"  [2] верификация упала: {e}")
        return False
    ok = r2.status_code == 200
    print(f"  [2] comments.json с cookies: {r2.status_code}")
    if ok:
        try:
            n = len(r2.json()["data"]["children"])
            print(f"      комментариев в ответе: {n}")
        except Exception:
            print(f"      200, но не JSON: {snippet(r2)}")
            ok = False
    else:
        print(f"      тело: {snippet(r2)}")

    # 2b. повторный запрос — не одноразовые ли cookies
    if ok and args.repeat:
        time.sleep(args.pause)
        r3 = sess.get(verify_url, params={"limit": 5})
        print(f"  [2b] повторный запрос: {r3.status_code}")
        ok = ok and r3.status_code == 200

    print(f"  ИТОГ: {'РАБОТАЕТ без браузера' if ok else 'НЕ прошло'}")

    # 3. запись
    if ok and args.write:
        path = ROOT / acc["cookie_file"]
        atomic_write_json(path, rows)
        print(f"  записано: {path} ({len(rows)} шт.)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", action="append", help="слот (можно повторять)")
    ap.add_argument("--all-enabled", action="store_true", help="все enabled: true из accounts.yaml")
    ap.add_argument("--write", action="store_true", help="при успехе записать cookies/account_N.json")
    ap.add_argument("--no-proxy", action="store_true", help="без прокси (только для дебага)")
    ap.add_argument("--subreddit", default="AskReddit")
    ap.add_argument("--extra-path", action="append",
                    help="доп. страницы для прогрева после / (напр. /r/AskReddit/), можно повторять")
    ap.add_argument("--repeat", action="store_true", help="повторить запрос через --pause сек")
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=20)
    args = ap.parse_args()

    accs = {a["name"]: a for a in load_accounts()}
    if args.all_enabled:
        names = [n for n, a in accs.items() if a.get("enabled")]
    elif args.account:
        names = args.account
    else:
        ap.error("укажи --account или --all-enabled")

    missing = [n for n in names if n not in accs]
    if missing:
        sys.exit(f"нет в accounts.yaml: {', '.join(missing)}")

    results = {}
    for i, n in enumerate(names):
        if i:
            time.sleep(args.pause)
        results[n] = test_slot(accs[n], args)

    print("\n--- сводка ---")
    for n, ok in results.items():
        print(f"  {n}: {'OK' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
