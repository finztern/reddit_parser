#!/usr/bin/env python3
"""
scripts/sync_subscription.py — суточная синхронизация VLESS-подписки с закреплёнными за аккаунтами нодами.

Подписка — текстовый список vless://-ссылок (или base64 такого списка). Узел определяется МЕТКОЙ — первым
сегментом пути (`path=/de2/` -> `de2`; если пути нет, берётся начало #имени до первой точки). За аккаунтом
закрепляется конкретная метка: её текущее значение — это метка ссылки аккаунта в vless_accounts.env.

Что делает (по умолчанию — ТОЛЬКО план: скачивает, сравнивает, печатает; запись — с --apply):

  1. Скачивает подписку. Ссылка с токеном — секрет: берётся из переменной NODE_SUBSCRIPTION_URL или из файла
     subscription.url в корне проекта (одна строка; файл в .gitignore/.dockerignore). Ссылку и ссылки узлов
     скрипт никогда не печатает.
  2. Предохранитель: подписка с числом узлов меньше --min-nodes (10) или меньше половины ранее виденных
     считается сбойной — ничего не меняется.
  3. Для каждого аккаунта, чья ссылка принадлежит подписке (тот же сервер/SNI, либо метка из подписки):
       * метка есть в подписке, а ссылка изменилась (путь, uuid, хост) — ссылка обновляется, имя в mihomo
         и закрепление за меткой сохраняются;
       * метки в подписке больше нет — аккаунту выдаётся свободный узел ТОЙ ЖЕ страны (сначала из
         all_nodes.reddit_ok.txt, затем по порядку меток); другая страна не берётся (профиль/locale аккаунта
         привязаны к стране; --cross-country разрешает любой свободный узел); нет подходящего — аккаунт
         остаётся как есть, в отчёте предупреждение.
  4. Пул all_nodes.txt: ссылки подписки в нём заменяются свежими, остальные строки сохраняются.
  5. Если что-то изменилось: бэкапы *.bak-sub-<время> (последние 5), запись vless_accounts.env и all_nodes.txt,
     generate_mihomo_config.py, `docker compose restart mihomo`. Ошибка генератора — откат файлов.
     Состояние (метки, серверы) — state/subscription_state.json.

    venv/bin/python3 scripts/sync_subscription.py            # план
    venv/bin/python3 scripts/sync_subscription.py --apply    # применить (так запускает systemd-таймер)

Коды выхода: 0 — ок (в т.ч. «без изменений»), 1 — ошибка применения, 2 — подписка недоступна/сбойная,
3 — ссылка на подписку не настроена.
Если запущен rotation-daemon, после смены ссылок аккаунтов перезапусти его (реестр хранит ключи нод).
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import generate_mihomo_config as gmc  # noqa: E402
import rotation as rot  # noqa: E402

URL_FILE = "subscription.url"
URL_ENV = "NODE_SUBSCRIPTION_URL"
STATE_FILE = "subscription_state.json"
MIN_NODES = 10
KEEP_BACKUPS = 5
DEFAULT_RESTART = "docker compose restart mihomo"
VLESS_RE = re.compile(r"vless://[^\s\"']+")


# ---------------------------------------------------------------- #
#  Разбор подписки и ссылок
# ---------------------------------------------------------------- #

def _num(name: str) -> int:
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def _natural(label: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", label)]


def label_of(uri: str) -> str:
    """Метка узла: первый сегмент path (`/de2/` -> de2), иначе начало #имени до точки/пробела."""
    sp = urllib.parse.urlsplit(uri)
    path = urllib.parse.unquote((urllib.parse.parse_qs(sp.query).get("path") or [""])[0]).strip("/")
    seg = path.split("/")[0].strip().lower() if path else ""
    if seg:
        return seg
    frag = urllib.parse.unquote(sp.fragment).strip().lower()
    return re.split(r"[.\s]", frag, maxsplit=1)[0] if frag else ""


def sig_of(uri: str) -> str:
    """Сервер подписки: host:port|sni — общий у всех узлов одного провайдера."""
    sp = urllib.parse.urlsplit(uri)
    q = urllib.parse.parse_qs(sp.query)
    sni = (q.get("sni") or q.get("host") or [""])[0]
    return f"{(sp.hostname or '').lower()}:{sp.port or ''}|{sni.lower()}"


def parse_subscription(text: str) -> tuple[dict[str, str], dict]:
    """Текст подписки -> ({метка: ссылка}, статистика). Битые ссылки и повтор метки пропускаются."""
    stats = {"links": 0, "bad": 0, "dups": 0}
    links = VLESS_RE.findall(text or "")
    if not links:
        try:
            raw = (text or "").strip()
            dec = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
            links = VLESS_RE.findall(dec)
        except Exception:  # noqa: BLE001
            links = []
    out: dict[str, str] = {}
    for u in links:
        stats["links"] += 1
        label = label_of(u)
        try:
            gmc.parse_vless(u, "x")
        except ValueError:
            stats["bad"] += 1
            continue
        if not label:
            stats["bad"] += 1
            continue
        if label in out:
            stats["dups"] += 1
            continue
        out[label] = u
    return out, stats


def read_url(root: Path, cli: str | None = None) -> str | None:
    if cli:
        return cli.strip()
    env = os.environ.get(URL_ENV, "").strip()
    if env:
        return env
    f = root / URL_FILE
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 — URL задаёт оператор
        return r.read().decode("utf-8", "ignore")


# ---------------------------------------------------------------- #
#  План
# ---------------------------------------------------------------- #

def read_env_links(text: str) -> dict[str, str]:
    out = {}
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = gmc.ACCOUNT_LINE_RE.match(raw)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def replace_env_line(text: str, account: str, uri: str) -> str:
    pat = re.compile(rf"(?m)^(\s*){re.escape(account)}\s*=.*$")
    if pat.search(text):
        return pat.sub(lambda m: f"{m.group(1)}{account} = {uri}", text, count=1)
    return text.rstrip("\n") + f"\n{account} = {uri}\n"


def _unique(uri: str, taken: set[str]) -> str:
    base = gmc.proxy_name(uri)
    name, i = base, 2
    while name in taken:
        name = f"{base}~{i}"
        i += 1
    taken.add(name)
    if name != base:
        uri = uri.split("#", 1)[0] + "#" + urllib.parse.quote(name, safe="~._-")
    return uri


def plan_sync(env_links: dict[str, str], sub: dict[str, str], known_labels=(), known_sigs=(),
              ok_rank: dict[str, int] | None = None, cross_country: bool = False) -> tuple[dict, list]:
    """-> ({account: новая_ссылка}, действия). Действие: {account, kind: update|replace|no_replacement,
    from, to}. Метки/страны — не секрет, ссылки в действия не попадают."""
    ok_rank = ok_rank or {}
    sigs = {sig_of(u) for u in sub.values()} | set(known_sigs)
    labels = set(sub) | set(known_labels)

    def is_sub(link: str) -> bool:
        return link.lower().startswith("vless://") and (sig_of(link) in sigs or label_of(link) in labels)

    mine = {a: l for a, l in env_links.items() if is_sub(l)}
    held = {label_of(l) for l in mine.values()}
    names = {gmc.proxy_name(l) for l in env_links.values()}
    new_links: dict[str, str] = {}
    actions: list[dict] = []
    vanished: list[str] = []
    for acct in sorted(mine, key=_num):
        link, lab = mine[acct], label_of(mine[acct])
        if lab not in sub:
            vanished.append(acct)
            continue
        fresh = sub[lab]
        if rot.node_key(fresh) != rot.node_key(link):
            frag = urllib.parse.urlsplit(link).fragment or urllib.parse.urlsplit(fresh).fragment
            new_links[acct] = rot.node_key(fresh) + (f"#{frag}" if frag else "")
            actions.append({"account": acct, "kind": "update", "from": lab, "to": lab})
    for acct in vanished:
        lab = label_of(mine[acct])
        cc = rot.country_for(lab)
        free = [l for l in sub if l not in held]
        same = [l for l in free if cc != "??" and rot.country_for(l) == cc]
        cands = free if cross_country else same
        cands.sort(key=lambda l: (ok_rank.get(rot.node_key(sub[l]), 10 ** 9), _natural(l)))
        if not cands:
            actions.append({"account": acct, "kind": "no_replacement", "from": lab, "to": None})
            continue
        pick = cands[0]
        held.add(pick)
        new_links[acct] = _unique(sub[pick], names)
        actions.append({"account": acct, "kind": "replace", "from": lab, "to": pick})
    return new_links, actions


def merge_pool(existing: list[str], sub: dict[str, str], known_sigs=()) -> list[str]:
    """Пул: ссылки подписки (по серверу) заменяются свежими, прочие узлы остаются на месте."""
    sigs = {sig_of(u) for u in sub.values()} | set(known_sigs)
    kept = [u for u in existing if not (u.lower().startswith("vless://") and sig_of(u) in sigs)]
    return kept + list(sub.values())


# ---------------------------------------------------------------- #
#  Применение
# ---------------------------------------------------------------- #

def _run(cmd: list[str], cwd: Path) -> int:
    return subprocess.run(cmd, cwd=str(cwd), check=False).returncode


def _backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.bak-sub-{stamp}")
    shutil.copy2(path, dst)
    old = sorted(path.parent.glob(f"{path.name}.bak-sub-*"))
    for p in old[:-KEEP_BACKUPS]:
        try:
            p.unlink()
        except OSError:
            pass
    return dst


def _load_state(root: Path) -> dict:
    try:
        d = json.loads((root / "state" / STATE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _pool_path(root: Path) -> Path:
    nf = Path(rot.DEFAULT_OPTS["nodes_file"])
    return nf if nf.is_absolute() else root / nf


def _ok_rank(nf: Path) -> dict[str, int]:
    ok = nf.with_name(nf.stem + ".reddit_ok.txt")
    return {n.key: i for i, n in enumerate(rot.load_pool(ok))} if ok.exists() else {}


def _describe(a: dict) -> str:
    if a["kind"] == "update":
        return f"{a['account']}: {a['from']} — ссылка обновлена (метка та же)"
    if a["kind"] == "replace":
        return f"{a['account']}: {a['from']} пропал из подписки -> {a['to']}"
    return f"{a['account']}: {a['from']} пропал из подписки, свободной ноды той же страны нет — оставлен как есть"


def sync(root: Path, url: str, *, apply: bool, min_nodes: int = MIN_NODES, cross_country: bool = False,
         restart_cmd: str | None = DEFAULT_RESTART, fetcher=fetch, run=_run, out=print) -> int:
    try:
        text = fetcher(url)
    except Exception as e:  # noqa: BLE001 — без текста ошибки: в нём может быть URL с токеном
        code = getattr(e, "code", None)
        out(f"ОШИБКА: подписка недоступна ({type(e).__name__}{f' {code}' if code else ''}) — ничего не менял")
        return 2
    sub, st = parse_subscription(text)
    state = _load_state(root)
    known = set(state.get("labels") or [])
    out(f"Подписка: узлов {len(sub)} (битых {st['bad']}, дублей метки {st['dups']})")
    if len(sub) < min_nodes or (known and len(sub) < len(known) / 2):
        out(f"ОШИБКА: в подписке слишком мало узлов ({len(sub)}; порог {min_nodes}, ранее {len(known)}) — "
            "похоже на сбой провайдера, ничего не менял")
        return 2

    env_path = root / "vless_accounts.env"
    if not env_path.exists():
        out("ОШИБКА: нет vless_accounts.env")
        return 2
    env_text = env_path.read_text(encoding="utf-8")
    env_links = read_env_links(env_text)
    nf = _pool_path(root)
    new_links, actions = plan_sync(env_links, sub, known, state.get("sigs") or [], _ok_rank(nf), cross_country)

    for a in actions:
        out(("  " if apply else "  [план] ") + _describe(a))
    added, gone = sorted(set(sub) - known), sorted(known - set(sub))
    if known:
        out(f"Метки: новых {len(added)}{(' (' + ', '.join(added[:12]) + ')') if added else ''}, "
            f"пропавших {len(gone)}{(' (' + ', '.join(gone[:12]) + ')') if gone else ''}")

    pool_old = [n.uri for n in rot.load_pool(nf)]
    pool_new = merge_pool(pool_old, sub, state.get("sigs") or [])
    pool_changed = set(pool_old) != set(pool_new)
    changed = bool(new_links) or pool_changed
    if not changed:
        out("Изменений нет.")
        if apply:
            _save_state(root, sub, actions)
        return 0
    if not apply:
        out(f"\n[план] ничего не записано (ссылок аккаунтов к смене: {len(new_links)}, "
            f"пул {'обновится' if pool_changed else 'без изменений'}). Для записи --apply")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = {p: _backup(p, stamp) for p in (env_path, nf)}
    try:
        new_env = env_text
        for acct, link in new_links.items():
            new_env = replace_env_line(new_env, acct, link)
        if new_links:
            rot._atomic_write(env_path, new_env)
        if pool_changed:
            head = "# сгенерировано scripts/sync_subscription.py — ссылки подписки обновляются, прочие узлы сохраняются\n"
            rot._atomic_write(nf, head + "\n".join(pool_new) + "\n")
        if run([sys.executable, str(SCRIPTS / "generate_mihomo_config.py")], root) != 0:
            raise RuntimeError("generate_mihomo_config.py завершился с ошибкой")
    except Exception as e:  # noqa: BLE001
        out(f"ОШИБКА применения: {e} — откатываю файлы из бэкапов")
        for path, bak in backups.items():
            if bak and bak.exists():
                shutil.copy2(bak, path)
        return 1
    _save_state(root, sub, actions)
    out(f"OK: ссылок аккаунтов обновлено {len(new_links)}, пул {'обновлён' if pool_changed else 'без изменений'} "
        f"(бэкапы *.bak-sub-{stamp})")
    if restart_cmd:
        rc = run(shlex.split(restart_cmd), root)
        out("mihomo перезапущен" if rc == 0 else f"ВНИМАНИЕ: `{restart_cmd}` вернул код {rc} — перезапусти mihomo вручную")
        if rc != 0:
            return 1
    else:
        out("Перезапуск пропущен (--no-restart): docker compose restart mihomo")
    if new_links:
        out("Если запущен rotation-daemon — перезапусти его (реестр хранит ключи нод слотов).")
    return 0


def _save_state(root: Path, sub: dict[str, str], actions: list[dict]) -> None:
    d = root / "state"
    d.mkdir(parents=True, exist_ok=True)
    data = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "count": len(sub),
            "labels": sorted(sub, key=_natural), "sigs": sorted({sig_of(u) for u in sub.values()}),
            "last_changes": actions}
    rot._atomic_write(d / STATE_FILE, json.dumps(data, ensure_ascii=False, indent=1))


def main(argv=None, *, fetcher=fetch, run=_run) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="записать и перезапустить mihomo (без флага — только план)")
    ap.add_argument("--root", default=None)
    ap.add_argument("--url", default=None, help=f"ссылка на подписку (иначе ${URL_ENV} или файл {URL_FILE})")
    ap.add_argument("--min-nodes", type=int, default=MIN_NODES)
    ap.add_argument("--cross-country", action="store_true",
                    help="при пропаже метки разрешить замену узлом ДРУГОЙ страны")
    ap.add_argument("--no-restart", action="store_true", help="не перезапускать mihomo")
    ap.add_argument("--restart-cmd", default=DEFAULT_RESTART)
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else rot.ROOT
    url = read_url(root, args.url)
    if not url:
        print(f"Подписка не настроена: положи ссылку в {URL_FILE} (корень проекта) или в ${URL_ENV}")
        return 3
    (root / "state").mkdir(parents=True, exist_ok=True)
    with open(root / "state" / "sync_subscription.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Другая синхронизация уже идёт — выхожу.")
            return 0
        return sync(root, url, apply=args.apply, min_nodes=args.min_nodes, cross_country=args.cross_country,
                    restart_cmd=None if args.no_restart else args.restart_cmd, fetcher=fetcher, run=run)


if __name__ == "__main__":
    sys.exit(main())
