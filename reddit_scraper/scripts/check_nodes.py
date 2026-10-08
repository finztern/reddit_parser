#!/usr/bin/env python3
"""
scripts/check_nodes.py — жива ли цепочка mihomo -> VLESS-нода -> Reddit.

    python3 scripts/check_nodes.py                    # все порты из mihomo_config.yaml
    python3 scripts/check_nodes.py --account account_5 --account account_10
    python3 scripts/check_nodes.py --enabled-only     # только enabled: true из accounts.yaml

Слои (каждый локализует свою причину):
  0. напрямую с хоста (без прокси): интернет и Reddit с IP самого сервера
  1. локальные порты mihomo открыты?
  2. VLESS-сервер достижим (TCP + TLS-рукопожатие с SNI)?
  3. через каждый порт: нейтральный сайт (Cloudflare trace) -> нода жива, выходной IP
  4. через каждый порт: Reddit (главная и comments.json без cookies)
  5. встроенный delay-тест mihomo (external-controller :9090), если доступен
"""

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

try:
    from curl_cffi import requests as cffi
except ImportError:
    sys.exit("curl_cffi не установлен — запускай тем же python, где он стоит (venv).")

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
IMP = "chrome131"  # нейтральный, заведомо существующий профиль


def load_yaml(p: Path):
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        sys.exit(f"не прочитал {p}: {e}")


def get(url, port=None, timeout=15):
    t0 = time.time()
    px = {"http": f"http://{HOST}:{port}", "https": f"http://{HOST}:{port}"} if port else None
    try:
        r = cffi.get(url, impersonate=IMP, proxies=px, timeout=timeout)
        return r.status_code, r.text, time.time() - t0, ""
    except Exception as e:
        return None, "", time.time() - t0, str(e)[:90]


def trace_ip(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("ip="):
            return line[3:]
    return "?"


def tcp_tls(server, port, sni, timeout=8):
    t0 = time.time()
    try:
        s = socket.create_connection((server, port), timeout=timeout)
    except Exception as e:
        return False, f"TCP: {e}", time.time() - t0
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(s, server_hostname=sni, do_handshake_on_connect=True):
            pass
        return True, "TCP+TLS ok", time.time() - t0
    except Exception as e:
        return False, f"TLS: {e}", time.time() - t0
    finally:
        s.close()


def port_open(port, timeout=2):
    try:
        socket.create_connection((HOST, port), timeout=timeout).close()
        return True
    except Exception:
        return False


def mihomo_delay(ctrl, node, url="https://www.reddit.com", timeout_ms=8000):
    q = urllib.parse.quote(node, safe="")
    u = f"http://{ctrl}/proxies/{q}/delay?url={urllib.parse.quote(url, safe='')}&timeout={timeout_ms}"
    try:
        with urllib.request.urlopen(u, timeout=timeout_ms / 1000 + 3) as r:
            return json.loads(r.read()).get("delay")
    except Exception as e:
        return f"ERR {str(e)[:40]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", action="append")
    ap.add_argument("--enabled-only", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    mih = load_yaml(ROOT / "mihomo_config.yaml")
    accs = {a["name"]: a for a in load_yaml(ROOT / "accounts.yaml").get("accounts", [])}
    proxies = {p["name"]: p for p in mih.get("proxies", [])}
    # ВАЖНО: в группе первая нода — назначенная, остальные (пул для ротации)
    # идут после неё; "now" с учётом ротации смотри в API mihomo.
    groups = {g["name"]: g["proxies"][0] for g in mih.get("proxy-groups", [])}
    ctrl = mih.get("external-controller", "127.0.0.1:9090")

    rows = []  # name, port, node
    for l in mih.get("listeners", []):
        acc = l["name"].replace("mixed-", "")
        if args.account and acc not in args.account:
            continue
        if args.enabled_only and not (accs.get(acc) or {}).get("enabled"):
            continue
        rows.append((acc, l["port"], groups.get(l["proxy"], "?")))
    rows.sort(key=lambda r: int(r[0].split("_")[1]))
    if not rows:
        sys.exit("нечего проверять (нет listeners в mihomo_config.yaml / фильтр пуст)")

    # ---- 0. напрямую
    print("=" * 72)
    print("0. НАПРЯМУЮ С ХОСТА (без прокси)")
    print("=" * 72)
    s, t, dt, err = get("https://www.cloudflare.com/cdn-cgi/trace")
    host_net = s == 200
    print(f"  интернет: {'ok, IP сервера=' + trace_ip(t) if host_net else 'НЕТ — ' + err} ({dt:.1f}s)")
    s, _, dt, err = get("https://www.reddit.com/")
    print(f"  Reddit напрямую: {s or 'ERR ' + err} ({dt:.1f}s)")

    # ---- 1. порты
    print()
    print("=" * 72)
    print("1. ЛОКАЛЬНЫЕ ПОРТЫ MIHOMO")
    print("=" * 72)
    opened = {port: port_open(port) for _, port, _ in rows}
    n_open = sum(opened.values())
    print(f"  открыто {n_open}/{len(rows)}")
    if n_open == 0:
        print("  [!] НИ ОДИН порт не слушает — mihomo не запущен/упал/не тот PROXY_HOST.")
        print("      docker compose ps ; docker compose logs --tail 50 mihomo")
        return 1
    for acc, port, _ in rows:
        if not opened[port]:
            print(f"  [!] {acc}: порт {port} закрыт")

    # ---- 2. upstream
    print()
    print("=" * 72)
    print("2. VLESS-СЕРВЕР (TCP + TLS с SNI)")
    print("=" * 72)
    ups = {}
    for node in {n for _, _, n in rows}:
        p = proxies.get(node)
        if p:
            ups[(p["server"], p["port"], p.get("servername", ""))] = None
    up_ok = {}
    for key in ups:
        okk, msg, dt = tcp_tls(*key)
        up_ok[key] = okk
        print(f"  {key[0]}:{key[1]} sni={key[2][:28]}…  {msg} ({dt:.1f}s)")

    # ---- 3-4. через каждый порт
    def probe(row):
        acc, port, node = row
        if not opened[port]:
            return row, None, None, None
        a = get("https://www.cloudflare.com/cdn-cgi/trace", port, 15)
        b = get("https://www.reddit.com/", port, 20)
        c = get("https://www.reddit.com/r/all/comments.json?limit=5", port, 20)
        return row, a, b, c

    print()
    print("=" * 72)
    print("3-4. ЧЕРЕЗ КАЖДЫЙ ПОРТ: нейтральный сайт / reddit.com / comments.json (без cookies)")
    print("=" * 72)
    res = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for row, a, b, c in ex.map(probe, rows):
            res.append((row, a, b, c))
            acc, port, node = row
            if a is None:
                print(f"  {acc:11} {port} {node:18} порт закрыт")
                continue
            net = f"ip={trace_ip(a[1])}" if a[0] == 200 else f"ERR {a[3] or a[0]}"
            rm = b[0] or f"ERR({b[3][:30]})"
            rj = c[0] or f"ERR({c[3][:30]})"
            print(f"  {acc:11} {port} {node:18} нода[{net}] reddit/[{rm}] json[{rj}] {b[2]:.1f}s")

    # ---- 5. delay mihomo
    print()
    print("=" * 72)
    print(f"5. DELAY-ТЕСТ САМОГО MIHOMO ({ctrl}) до reddit.com, мс")
    print("=" * 72)
    d_any = False
    for acc, port, node in rows[:6]:
        d = mihomo_delay(ctrl, node)
        d_any = d_any or isinstance(d, int)
        print(f"  {acc:11} {node:18} {d}")
    if not d_any:
        print("  (API mihomo недоступен или все таймауты — не критично, слои 3-4 важнее)")

    # ---- итог
    print()
    print("=" * 72)
    print("ИТОГ")
    print("=" * 72)
    live = [r for r in res if r[1] is not None]
    net_ok = [r for r in live if r[1][0] == 200]
    rd_ok = [r for r in live if r[2][0] is not None]      # любой HTTP-ответ = достижимо
    json_codes = {}
    for r in live:
        k = r[3][0] if r[3][0] else "timeout/err"
        json_codes[k] = json_codes.get(k, 0) + 1

    if not host_net:
        print("  У самого хоста нет интернета — проблема не в нодах.")
    elif up_ok and not any(up_ok.values()):
        print("  VLESS-сервер недостижим с хоста (TCP/TLS) — упал, заблокирован или подписка Trust.Zone "
              "истекла. Все ноды у вас на одном сервере, поэтому ломаются все разом.")
        print("  Проверь подписку по ссылке из vless_accounts.env и доступность сервера с другой сети.")
    elif not net_ok:
        print("  Порты открыты и сервер отвечает, но через ни один порт не ходит даже нейтральный сайт — "
              "ломается сама цепочка mihomo (path/host/uuid в конфиге, истёкший доступ).")
        print("  docker compose logs --tail 100 mihomo — ищи ошибки vless/ws/httpupgrade.")
    elif len(net_ok) < len(live):
        bad = [r[0][0] for r in live if r[1][0] != 200]
        print(f"  Мёртвые ноды (нейтральный сайт не открывается): {', '.join(bad)} — замени в vless_accounts.env.")
    if net_ok and not rd_ok:
        print("  Ноды живы (нейтральный сайт открывается), но Reddit не отвечает НИ через одну — "
              "соединение молча отбрасывается (таймаут), а не 403. Так выглядит сетевая блокировка "
              "IP-диапазона Reddit'ом/провайдером.")
        print("  Если напрямую с хоста Reddit открывается, а через ноды нет — диапазон нод Trust.Zone забанен.")
    elif rd_ok:
        print(f"  Reddit достижим через {len(rd_ok)}/{len(live)} живых нод. "
              f"comments.json без cookies: {json_codes}")
        print("  (403 без cookies — норма с мая 2026; 429 — IP троттлится; 200 — вообще чисто.)")
        if len(rd_ok) == len(live) and len(net_ok) == len(live):
            print("  => Сеть и IP в порядке. Если refresh_guest_cookies всё равно ловит таймаут — виноват не "
                  "IP, а ожидание страницы в Playwright: wait_until=\"networkidle\" на reddit.com часто "
                  "не наступает. Замени в refresh_guest_cookies.py на wait_until=\"domcontentloaded\" "
                  "(пауза post_load_settle_seconds после загрузки уже есть).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
