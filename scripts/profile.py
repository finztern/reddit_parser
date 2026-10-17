#!/usr/bin/env python3
"""
scripts/profile.py — постоянные профили устройств аккаунтов (модуль 03).

Профиль создаётся ОДИН раз (state/profiles/account_N.json) и дальше не меняется. Без --apply
скрипт только показывает план, ничего не пишет.

    # профили для account_1..10 из accounts.yaml: TLS-отпечаток, UA и locale НЕ меняются
    venv/bin/python3 scripts/profile.py --migrate --accounts 1-10 [--apply]

    # новые профили (генератор + пресеты), гео — из выходного IP ноды через прокси слота
    venv/bin/python3 scripts/profile.py --create --accounts 11-55 [--seed 42] [--apply]
    venv/bin/python3 scripts/profile.py --create --accounts 11-55 --offline   # гео из accounts.yaml, без сети

    # перепроверить гео (смена ноды): правит ТОЛЬКО гео-группу профиля
    venv/bin/python3 scripts/profile.py --refresh-geo --accounts 1-10 [--apply]

    venv/bin/python3 scripts/profile.py --list | --show account_N | --validate
    venv/bin/python3 scripts/profile.py --reset account_N [--yes] [--apply]   # осознанная замена

--reset меняет отпечаток устройства: сессия Reddit может слететь (новый «человек» с того же IP),
понадобится ручной вход заново. Старый профиль сохраняется в state/profiles/.history/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import geoip, profile_gen as pg  # noqa: E402
from scraper.config import load_accounts  # noqa: E402
from scraper.constants import BASE_DIR, PROXY_HOST  # noqa: E402
from scraper.identity import STATE_DIR  # noqa: E402

REFRESH_COOKIES_YAML = BASE_DIR / "scripts" / "refresh_cookies.yaml"


def parse_accounts_arg(spec: list[str] | None, all_names: list[str]) -> list[str]:
    """'1-10', '11 12', 'account_5', 'all' -> ['account_1', ...] (только существующие в accounts.yaml)."""
    if not spec or spec == ["all"]:
        return list(all_names)
    out: list[str] = []
    for tok in spec:
        for part in tok.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part and not part.startswith("account_"):
                a, b = part.split("-", 1)
                out += [f"account_{i}" for i in range(int(a), int(b) + 1)]
            elif part.isdigit():
                out.append(f"account_{part}")
            else:
                out.append(part)
    unknown = [n for n in out if n not in all_names]
    if unknown:
        sys.exit(f"нет в accounts.yaml: {', '.join(unknown)}")
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def offline_geo(acc: dict, refresh_cfg: dict) -> dict:
    """Гео без сети: locale из accounts.yaml (+ timezone из refresh_cookies.yaml:account_geo)."""
    locale = (acc.get("locale") or "").replace("_", "-")
    region = locale.split("-")[-1].upper() if "-" in locale else None
    geo = geoip.geo_for_country(region)
    if geo["geo_known"] and locale:
        geo["locale"] = locale                      # оставляем locale аккаунта как есть
        geo["languages"] = geoip.languages_for(locale)
    tz = ((refresh_cfg.get("account_geo") or {}).get(acc["name"]) or {}).get("timezone_id")
    if tz and geoip.valid_timezone(tz):
        geo["timezone_id"] = tz
    geo.update({"geo_source": "accounts.yaml", "exit_ip": None, "checked_at": int(time.time())})
    return geo


def live_geo(acc: dict) -> dict:
    port = acc.get("proxy_port")
    if not port:
        raise RuntimeError("нет proxy_port в accounts.yaml")
    return geoip.resolve_geo(f"http://{PROXY_HOST}:{port}")


def _row(p: dict) -> str:
    scr = p["screen"]
    return (f"{p['account']:<11} {p['os']:<8} {p['impersonate']:<18} {p['country'] or '??':<3} "
            f"{p['locale']:<7} {p['timezone_id']:<22} {scr['w']}x{scr['h']}@{p['device_pixel_ratio']:g} "
            f"{p['hardware_concurrency']}c/{p.get('ram_gb') or '?'}G {p['webgl_renderer'][:48]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Постоянные профили устройств аккаунтов (модуль 03)")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--create", action="store_true", help="создать недостающие профили генератором")
    act.add_argument("--migrate", action="store_true", help="профили из accounts.yaml без смены отпечатка")
    act.add_argument("--refresh-geo", action="store_true", help="перепроверить гео по выходному IP")
    act.add_argument("--reset", metavar="ACCOUNT", help="заменить профиль (сбрасывает отпечаток!)")
    act.add_argument("--list", action="store_true")
    act.add_argument("--show", metavar="ACCOUNT")
    act.add_argument("--validate", action="store_true", help="validate_profile() для всех файлов в state/profiles")
    ap.add_argument("--accounts", nargs="*", help="1-10 | 11 12 | account_5 | all (по умолчанию: migrate=1-10, прочее=all)")
    ap.add_argument("--seed", default="0", help="базовый seed генератора (одинаковый seed = те же профили)")
    ap.add_argument("--offline", action="store_true", help="гео из accounts.yaml, без запросов через прокси")
    ap.add_argument("--apply", action="store_true", help="записать (без него — только план)")
    ap.add_argument("--yes", action="store_true", help="подтвердить --reset без вопроса")
    ap.add_argument("--state-dir", default=str(STATE_DIR))
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    accounts = {a["name"]: a for a in load_accounts()}
    names_all = list(accounts)
    refresh_cfg = {}
    if REFRESH_COOKIES_YAML.exists():
        refresh_cfg = yaml.safe_load(REFRESH_COOKIES_YAML.read_text(encoding="utf-8")) or {}
    presets = pg.load_presets()

    # ---------------- просмотр
    if args.list or args.validate:
        rc = 0
        d = pg.profiles_dir(state_dir)
        files = sorted(d.glob("account_*.json"), key=lambda p: int(p.stem.split("_")[1])) if d.exists() else []
        for f in files:
            p = pg.load_profile(f.stem, state_dir)
            if p is None:
                print(f"{f.stem:<11} НЕЧИТАЕМ"); rc = 1; continue
            errs = pg.validate_profile(p, presets)
            if args.list:
                note = f"  [!] {p['geo_note']}" if p.get("geo_note") else ""
                print(_row(p) + note)
            if errs:
                rc = 1
                print(f"  ✗ {p['account']}: " + "; ".join(errs))
        missing = [n for n in names_all if not (d / f"{n}.json").exists()]
        print(f"\nпрофилей: {len(files)}; без профиля: {len(missing)}"
              + (f" ({', '.join(missing[:8])}{'…' if len(missing) > 8 else ''})" if missing else ""))
        return rc
    if args.show:
        p = pg.load_profile(args.show, state_dir)
        if p is None:
            print(f"{args.show}: профиля нет"); return 1
        print(json.dumps(p, ensure_ascii=False, indent=1))
        return 0

    # ---------------- reset
    if args.reset:
        name = args.reset
        if name not in accounts:
            sys.exit(f"{name} нет в accounts.yaml")
        old = pg.load_profile(name, state_dir)
        acc = accounts[name]
        geo = (old and {k: old[k] for k in pg.GEO_FIELDS if k in old}) or offline_geo(acc, refresh_cfg)
        seed = pg.derive_seed(args.seed, name, int(time.time()))
        new = pg.generate_profile(name, seed, geo=geo, presets=presets)
        print("Будет заменён профиль (старый уйдёт в state/profiles/.history/):")
        if old:
            print("  было:  " + _row(old))
        print("  станет: " + _row(new))
        print("\n⚠ Смена отпечатка: сессия Reddit может слететь — потребуется ручной вход "
              "(scripts/manual_login.py / login_hub.py).")
        if not args.apply:
            print("\n(план; для замены добавь --apply [--yes])"); return 0
        if not args.yes and input("Продолжить? [yes/N] ").strip().lower() != "yes":
            print("отменено"); return 1
        path = pg.reset_profile(name, new, state_dir, presets)
        print(f"записан {path}")
        return 0

    # ---------------- create / migrate / refresh-geo
    spec = args.accounts if args.accounts is not None else (["1-10"] if args.migrate else None)
    names = parse_accounts_arg(spec, names_all)
    d = pg.profiles_dir(state_dir)
    existing = {n: pg.load_profile(n, state_dir) for n in names}
    rc = 0

    if args.refresh_geo:
        for n in names:
            cur = existing[n]
            if cur is None:
                print(f"{n}: профиля нет — пропуск"); continue
            try:
                geo = offline_geo(accounts[n], refresh_cfg) if args.offline else live_geo(accounts[n])
            except Exception as e:
                print(f"{n}: гео не определено ({e}) — профиль не тронут"); rc = 1; continue
            reason = geoip.needs_recheck(cur, geo.get("exit_ip")) if geo.get("exit_ip") else "offline"
            changed = {k: (cur.get(k), geo[k]) for k in ("country", "locale", "timezone_id", "exit_ip") if cur.get(k) != geo.get(k)}
            print(f"{n}: {geo['country']} {geo['locale']} {geo['timezone_id']} ip={geo.get('exit_ip')} "
                  f"({reason or 'ok'}); изменения: {changed or 'нет'}"
                  + (f"  [!] {geo['geo_note']}" if geo.get("geo_note") else ""))
            if args.apply and changed:
                pg.update_geo(n, geo, state_dir, presets)
        if not args.apply:
            print("\n(план; для записи добавь --apply)")
        return rc

    todo = [n for n in names if existing[n] is None]
    skipped = [n for n in names if existing[n] is not None]
    if skipped:
        print(f"уже есть профиль (постоянный, не трогаю): {', '.join(skipped)}")
    taken = {pg.fingerprint(p) for p in (pg.load_profile(f.stem, state_dir) for f in
                                          (d.glob("account_*.json") if d.exists() else [])) if p}
    geos: dict[str, dict] = {}
    for n in todo:
        try:
            geos[n] = offline_geo(accounts[n], refresh_cfg) if (args.offline or args.migrate) else live_geo(accounts[n])
        except Exception as e:
            print(f"{n}: гео из IP не определено ({e}); беру из accounts.yaml и помечаю geo_source")
            geos[n] = offline_geo(accounts[n], refresh_cfg)
            geos[n]["geo_source"] = "accounts.yaml(fallback)"

    profiles: dict[str, dict] = {}
    if args.migrate:
        for n in todo:
            try:
                profiles[n] = pg.profile_from_account(accounts[n], geos[n], presets)
            except pg.ProfileError as e:
                print(f"{n}: {e}"); rc = 1
    else:
        profiles = pg.generate_fleet(todo, args.seed, geos, presets, taken=taken)

    for n, p in profiles.items():
        errs = pg.validate_profile(p, presets)
        print(_row(p) + (f"  ✗ {'; '.join(errs)}" if errs else "") + (f"  [!] {p['geo_note']}" if p.get("geo_note") else ""))
        if errs:
            rc = 1
    if args.apply:
        for n, p in profiles.items():
            if pg.validate_profile(p, presets):
                continue                        # несогласованный профиль не сохраняется
            print(f"записан {pg.create_profile(p, state_dir, presets)}")
    else:
        print(f"\n(план: {len(profiles)} профилей; для записи добавь --apply)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
