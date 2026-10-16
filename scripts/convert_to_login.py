#!/usr/bin/env python3
"""
scripts/convert_to_login.py — переводит гостевые слоты в ЛОГИН-режим (модуль 01).

Что делает со слотом account_N (по умолчанию — ТОЛЬКО печатает план; запись — с --apply):

  scripts/refresh_guest_cookies.yaml  убирает из guest_accounts (и строку из account_geo в шапке)
  config.yaml                         убирает из rotation.slots (гостевая ротация слот больше не трогает)
  scripts/refresh_cookies.yaml        добавляет в login_accounts и account_geo (гео — под ноду слота)
  accounts.yaml                       enabled: false (до первого входа), login: true,
                                      cookie_file как был, storage_state: storage_state/account_N.json
  config.yaml                         если нет секции fleet: — дописывает её (reserve_nodes и др.)

Слот без залогиненной сессии не стартует в скрапере (enabled: false), пока не войдёшь:
    venv/bin/python3 scripts/manual_login.py account_N      # после входа слот включится сам
Список «нужен вход»: manual_login.py --list (колонка «вход»).

Бэкапы: каждый изменённый файл копируется в <имя>.bak-login-<время>.
Откат: вернуть *.bak-login-* на место (или `git revert`) — гостевой режим снова работает.

Какие слоты:
    --accounts 11-55        диапазон / список (11-15,20)
    --all                   все слоты, что сейчас в guest_accounts
Уже логин-слоты пропускаются (скрипт идемпотентен).

Запуск (из корня проекта):
    venv/bin/python3 scripts/convert_to_login.py --all                # план
    venv/bin/python3 scripts/convert_to_login.py --all --apply        # запись
    venv/bin/python3 scripts/convert_to_login.py --all --root /tmp/copy --apply   # на копии репозитория
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import generate_mihomo_config as gmc  # noqa: E402
import login_fleet as lf  # noqa: E402
import rotation as rot  # noqa: E402

FLEET_SECTION = """
# --- парк логин-аккаунтов (модуль 01) ---
fleet:
  # годных нод (Reddit отдаёт 200), которые НЕ раздаются слотам — запас на замену сгоревших
  reserve_nodes: 6
  # меньше годных нод — предупреждение в отчёте filter_nodes.py и в rotation_daemon.py --status
  min_good_nodes_warn: 10
  # как часто filter_nodes.py перепроверяет пул (systemd-таймер filter-nodes.timer)
  node_check_interval_hours: 24
  # сгоревшая нода логин-слота: только ЗАМЕНА НОДЫ (профиль и сессия остаются)
  node_replace_only: true
  # гостевой харвест и ротация: код остаётся, но выключен
  guest_enabled: false
  # кандидатов на замену брать только из all_nodes.reddit_ok.txt (результат filter_nodes.py --write)
  use_reddit_ok_file: true
"""


def slot_geo(name: str, p: lf.Paths, guest_cfg: dict, login_cfg: dict, links: dict[str, str],
             acc: dict) -> tuple[dict, str]:
    """Гео слота -> (geo, откуда). Приоритет: активный профиль демона (state/slots) >
    account_geo гостя > guest_accounts[N].geo > по имени ноды > locale из accounts.yaml."""
    try:
        prof = json.loads((p.state / "slots" / f"{name}.json").read_text(encoding="utf-8"))
        if prof.get("geo"):
            return prof["geo"], "state/slots"
    except (OSError, ValueError):
        pass
    g = (guest_cfg.get("account_geo") or {}).get(name)
    if g:
        return g, "account_geo"
    g = ((guest_cfg.get("guest_accounts") or {}).get(name) or {}).get("geo")
    if g:
        return g, "guest_accounts.geo"
    link = links.get(name)
    if link:
        try:
            return rot.geo_for(gmc.parse_vless(link, name)["name"]), "имя ноды"
        except ValueError:
            pass
    dflt = login_cfg.get("geo_defaults") or {"locale": "en-US", "timezone_id": "America/New_York"}
    return {"locale": acc.get("locale") or dflt["locale"], "timezone_id": dflt["timezone_id"]}, "по умолчанию"


def convert(p: lf.Paths, slots: list[str] | None, all_guests: bool, apply: bool, stamp: str | None = None,
            out=print) -> int:
    accounts_text = p.accounts.read_text(encoding="utf-8")
    guest_text = p.guest.read_text(encoding="utf-8")
    config_text = p.config.read_text(encoding="utf-8")
    login_text = p.login.read_text(encoding="utf-8")
    accounts = (yaml.safe_load(accounts_text) or {}).get("accounts") or []
    guest_cfg = yaml.safe_load(guest_text) or {}
    login_cfg = yaml.safe_load(login_text) or {}
    guest = guest_cfg.get("guest_accounts") or {}
    by_name = {a["name"]: a for a in accounts}
    links = lf.load_links(p)

    if all_guests:
        want = sorted(guest, key=lf.acc_num)
    else:
        want = list(slots or [])
    unknown = [s for s in want if s not in by_name]
    if unknown:
        out(f"ОШИБКА: нет в accounts.yaml: {', '.join(unknown)}")
        return 2
    already = [s for s in want if s not in guest]
    todo = [s for s in want if s in guest]
    if already:
        out(f"Пропускаю (не гостевые, уже логин или не слот): {', '.join(already)}")
    if not todo:
        out("Переводить нечего.")
        return 0

    geo, src, labels = {}, {}, {}
    for s in todo:
        geo[s], src[s] = slot_geo(s, p, guest_cfg, login_cfg, links, by_name[s])
        link = links.get(s)
        if link:
            try:
                labels[s] = gmc.parse_vless(link, s)["name"]
            except ValueError:
                pass

    out(f"{'слот':<11} {'порт':<5} {'гео':<7} {'откуда гео':<19} нода")
    for s in todo:
        out(f"{s:<11} {by_name[s].get('proxy_port')!s:<5} {geo[s]['locale']:<7} {src[s]:<19} {labels.get(s, '— (нет в vless_accounts.env)')}")
    out(f"\nБудет переведено слотов: {len(todo)} (enabled: false до первого входа)")

    # --- новые тексты ---
    new_accounts = accounts_text
    for s in todo:
        new_accounts = lf.to_login_account(new_accounts, s, enabled=False)
    new_guest = lf.remove_guest_slots(guest_text, todo)
    new_config = lf.remove_config_slots(config_text, todo)
    if not isinstance(yaml.safe_load(config_text).get("fleet"), dict):
        new_config = new_config.rstrip("\n") + "\n" + FLEET_SECTION
    new_login = lf.patch_login_yaml(login_text, todo, geo, labels)

    # --- самопроверка до записи ---
    acc2 = (yaml.safe_load(new_accounts) or {}).get("accounts") or []
    guest2 = (yaml.safe_load(new_guest) or {}).get("guest_accounts") or {}
    login2 = yaml.safe_load(new_login) or {}
    cfg2 = yaml.safe_load(new_config) or {}
    problems = lf.validate_fleet(acc2, links, guest2, login2, (cfg2.get("rotation") or {}).get("slots") or [])
    if problems:
        out("ОШИБКА: результат не согласован — ничего не записываю:\n  " + "\n  ".join(problems))
        return 1

    if not apply:
        out("\n[план] ничего не записано. Для записи добавь --apply")
        return 0

    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    for path in (p.accounts, p.guest, p.config, p.login):
        shutil.copy2(path, path.with_name(f"{path.name}.bak-login-{stamp}"))
    for path, text in ((p.accounts, new_accounts), (p.guest, new_guest), (p.config, new_config),
                       (p.login, new_login)):
        _write_inplace(path, text)

    out(f"\nOK: записано (бэкапы *.bak-login-{stamp}): accounts.yaml, scripts/refresh_guest_cookies.yaml, "
        "config.yaml, scripts/refresh_cookies.yaml")
    out("\nДальше:")
    out("  1) sudo systemctl restart rotation-daemon        # демон подхватит режим node-replace-only")
    out("  2) войти в каждый слот: venv/bin/python3 scripts/manual_login.py account_N   (список: --list)")
    out("  3) docker compose up -d --build scraper          # после входа слот включится и стартует")
    out(f"Откат: вернуть файлы из *.bak-login-{stamp} (или git revert).")
    return 0


def _write_inplace(path: Path, text: str) -> None:
    """accounts.yaml и config.yaml примонтированы в контейнер как ОДИН файл —
    пишем в тот же inode, а не заменой файла."""
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--accounts", help="слоты: 11-55 или 11-15,20")
    g.add_argument("--all", action="store_true", help="все слоты из guest_accounts")
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только план)")
    ap.add_argument("--root", default=str(lf.ROOT), help="каталог проекта (по умолчанию — этот репозиторий)")
    args = ap.parse_args()
    try:
        slots = lf.parse_slot_spec(args.accounts) if args.accounts else None
    except ValueError as e:
        sys.exit(f"ОШИБКА: {e}")
    return convert(lf.Paths(Path(args.root).resolve()), slots, args.all, args.apply)


if __name__ == "__main__":
    sys.exit(main())
