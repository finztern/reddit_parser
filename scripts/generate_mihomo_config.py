#!/usr/bin/env python3
"""
Генерирует mihomo_config.yaml из:
  - proxy_accounts.env   — файл вида account_1 = http://user:pass@host:port
  - accounts.yaml        — берём оттуда proxy_port для каждого account_N
  - all_proxys.txt       — пул HTTP(S)/SOCKS5-прокси (по одному в строке) для
                           ротации (см. scripts/rotation_daemon.py)

Запуск (из корня проекта):

    python3 scripts/generate_mihomo_config.py

После этого (пере)запусти mihomo:

    docker compose restart mihomo

Формат proxy_accounts.env — по одной ссылке на аккаунт:

    account_1 = http://user:pass@host:port#имя
    account_2 = http://user:pass@host:port

Строки с "#" в начале и пустые строки игнорируются. Аккаунты, для
которых в proxy_accounts.env нет строки, не получат слушателя в mihomo —
их воркер не сможет подключиться. Это касается и гостевых слотов
account_11..25: пока для них нет ссылок, они должны быть enabled: false.

Если рядом лежит all_proxys.txt, его ноды добавляются в proxies, а ВСЕ
ноды (пул + ноды других аккаунтов) — в КАЖДУЮ группу pg-account_N (после
назначенной ноды) — тогда rotation_daemon.py сможет переключать ноду
через API mihomo без рестарта. Какие ноды слот вправе брать, решает
демон (ноды логин-аккаунтов гостям не отдаются).
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_PATH = ROOT / "accounts.yaml"
PROXY_ENV_PATH = ROOT / "proxy_accounts.env"
OUTPUT_PATH = ROOT / "mihomo_config.yaml"

ACCOUNT_LINE_RE = re.compile(r"^\s*(account_\d+)\s*=\s*(\S+)\s*$")


def load_account_ports() -> dict[str, int]:
    """account_1 -> 7892, ... — proxy_port из accounts.yaml."""
    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    ports = {}
    for acc in data.get("accounts", []):
        name = acc.get("name")
        port = acc.get("proxy_port")
        if name and port:
            ports[name] = int(port)
    return ports


def load_proxy_links() -> dict[str, str]:
    """account_1 -> 'http://user:pass@host:port' из proxy_accounts.env (некомментированные строки)."""
    links: dict[str, str] = {}
    if not PROXY_ENV_PATH.exists():
        # Чистый сервер: ссылок на слоты ещё нет, есть только пул нод
        # (all_proxys.txt) — mihomo поднимется с одним пулом, а ноды слотам
        # раздаст scripts/add_guest_slots.py. Если нет и пула — main() упадёт.
        print(f"ПРЕДУПРЕЖДЕНИЕ: нет {PROXY_ENV_PATH.name} — считаю, что слотам ещё не назначены ноды",
              file=sys.stderr)
        return links

    for raw_line in PROXY_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = ACCOUNT_LINE_RE.match(line)
        if not m:
            print(f"ПРЕДУПРЕЖДЕНИЕ: не смог разобрать строку в proxy_accounts.env: {raw_line!r}", file=sys.stderr)
            continue
        account, link = m.group(1), m.group(2)
        norm = normalize_proxy(link)
        if not norm:
            print(f"ПРЕДУПРЕЖДЕНИЕ: {account} — не похоже на прокси (user:pass@host:port), пропускаю", file=sys.stderr)
            continue
        links[account] = norm
    return links


PROXY_SCHEMES = ("http", "https", "socks5")


def normalize_proxy(raw: str) -> str | None:
    """Строка из all_proxys.txt -> каноничный URI `scheme://user:pass@host:port[#имя]`
    или None, если строка не похожа на прокси. Понимает:
      scheme://user:pass@host:port, user:pass@host:port, host:port:user:pass, host:port.
    Без схемы считается http. Хвост `#имя` (необязательный) сохраняется."""
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None
    line, _, frag = line.partition("#")
    line = line.strip()
    frag = frag.strip()
    scheme = "http"
    if "://" in line:
        scheme, _, line = line.partition("://")
        scheme = scheme.lower()
        if scheme not in PROXY_SCHEMES:
            return None
    if "@" in line:
        auth, _, hostport = line.rpartition("@")
        user, _, pwd = auth.partition(":")
    else:
        parts = line.split(":")
        if len(parts) == 2:
            hostport, user, pwd = line, "", ""
        elif len(parts) == 4:
            hostport, user, pwd = f"{parts[0]}:{parts[1]}", parts[2], parts[3]
        else:
            return None
    host, sep, port = hostport.rpartition(":")
    if not sep or not host or not port.isdigit() or not (0 < int(port) < 65536):
        return None
    if any(c in host for c in " /?@"):
        return None
    cred = ""
    if user or pwd:
        cred = (urllib.parse.quote(urllib.parse.unquote(user), safe="")
                + ":" + urllib.parse.quote(urllib.parse.unquote(pwd), safe="") + "@")
    return f"{scheme}://{cred}{host}:{int(port)}" + (f"#{frag}" if frag else "")


def proxy_name(uri: str) -> str:
    """Имя прокси в mihomo: #фрагмент, иначе px-host-port (без учётных данных)."""
    parsed = urllib.parse.urlsplit(uri)
    frag = urllib.parse.unquote(parsed.fragment).strip()
    return frag or f"px-{parsed.hostname}-{parsed.port}"


def parse_proxy(uri: str, fallback_name: str = "") -> dict:
    """Один URI прокси -> словарь для mihomo proxies[] (type http/socks5)."""
    norm = normalize_proxy(uri)
    if not norm:
        raise ValueError("не прокси-ссылка (ожидаю user:pass@host:port)")
    parsed = urllib.parse.urlsplit(norm)
    name = proxy_name(norm) if parsed.fragment else (fallback_name or proxy_name(norm))
    proxy: dict = {
        "name": name,
        "type": "socks5" if parsed.scheme == "socks5" else "http",
        "server": parsed.hostname,
        "port": int(parsed.port),
    }
    if parsed.username:
        proxy["username"] = urllib.parse.unquote(parsed.username)
        proxy["password"] = urllib.parse.unquote(parsed.password or "")
    if parsed.scheme == "https":
        proxy["tls"] = True
    return proxy


def build_config(account_ports: dict[str, int], account_links: dict[str, str]) -> dict:
    proxies = []
    proxy_groups = []
    listeners = []

    missing = [a for a in account_ports if a not in account_links]
    used = [a for a in account_links if a in account_ports]

    for account in sorted(used, key=lambda a: int(a.split("_")[1])):
        link = account_links[account]
        port = account_ports[account]
        try:
            proxy = parse_proxy(link, fallback_name=account)
        except ValueError as e:
            print(f"ОШИБКА в ссылке для {account}: {e}", file=sys.stderr)
            continue

        proxies.append(proxy)
        group_name = f"pg-{account}"
        proxy_groups.append({"name": group_name, "type": "select", "proxies": [proxy["name"]]})
        listeners.append({
            "name": f"mixed-{account}",
            "type": "mixed",
            "port": port,
            "proxy": group_name,
        })

    if missing:
        print(
            "ПРЕДУПРЕЖДЕНИЕ: для этих аккаунтов из accounts.yaml нет ссылки в proxy_accounts.env "
            f"(останутся без прокси, держи их enabled: false): {', '.join(sorted(missing, key=lambda a: int(a.split('_')[1])))}",
            file=sys.stderr,
        )

    unknown = [a for a in account_links if a not in account_ports]
    if unknown:
        print(
            "ПРЕДУПРЕЖДЕНИЕ: в proxy_accounts.env есть ссылки для аккаунтов, "
            f"которых нет в accounts.yaml (игнорирую): {', '.join(sorted(unknown))}",
            file=sys.stderr,
        )

    return {
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "external-controller": "127.0.0.1:9090",
        "proxies": proxies,
        "proxy-groups": proxy_groups,
        "listeners": listeners,
        "rules": ["MATCH,DIRECT"],
    }


HEADER = """\
# ============================================================
#  Автоматически сгенерировано скриптом scripts/generate_mihomo_config.py
#  НЕ РЕДАКТИРУЙ ЭТОТ ФАЙЛ РУКАМИ — правь proxy_accounts.env (и
#  all_proxys.txt) и запускай скрипт заново.
#
#  Запускать mihomo: mihomo -f mihomo_config.yaml
#  (mihomo = Clash.Meta core, https://github.com/MetaCubeX/mihomo)
#  Требует mihomo с поддержкой "listeners".
# ============================================================

"""


def main() -> None:
    account_ports = load_account_ports()
    account_links = load_proxy_links()
    config = build_config(account_ports, account_links)

    # Пул нод для ротации (опционально, нужен all_proxys.txt).
    # Импорт ленивый: rotation сам импортирует этот модуль.
    pool_added = 0
    try:
        import rotation  # noqa: WPS433
        pool_added = rotation.extend_with_pool(config)
    except Exception as e:  # нет fcntl (Windows), нет curl_cffi и т.п. — пул просто не подключаем
        print(f"ПРЕДУПРЕЖДЕНИЕ: пул нод для ротации не подключён ({e})", file=sys.stderr)

    # Ошибка — только если нет НИ одной ноды: ни назначенной слоту, ни в пуле.
    # (Раньше проверка шла до пула, и на чистом сервере без ссылок в
    # proxy_accounts.env mihomo нельзя было поднять, чтобы проверить ноды.)
    if not config["proxies"]:
        print("ОШИБКА: не удалось собрать ни одного прокси — проверь proxy_accounts.env и all_proxys.txt",
              file=sys.stderr)
        sys.exit(1)

    yaml_text = yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120)
    OUTPUT_PATH.write_text(HEADER + yaml_text, encoding="utf-8")

    n_assigned = len(config["listeners"])
    print(f"OK: записал {OUTPUT_PATH} — {n_assigned} аккаунт(ов) с нодой"
          + (f", + {pool_added} нод пула для ротации" if pool_added else "") + ":")
    for pg in config["proxy-groups"]:
        acc = pg["name"].removeprefix("pg-")
        port = next(l["port"] for l in config["listeners"] if l["proxy"] == pg["name"])
        print(f"  {acc}: локальный порт {port} -> {pg['proxies'][0]}")


if __name__ == "__main__":
    main()
