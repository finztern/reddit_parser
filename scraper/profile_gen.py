"""
Постоянный профиль устройства аккаунта (модуль 03).

Профиль создаётся один раз (state/profiles/account_N.json) и дальше НЕ меняется — кроме
гео-группы (country/locale/timezone_id/languages/geo_source/exit_ip/checked_at), которую
обновляет только update_geo() при смене ноды. Полная замена — только reset_profile()
(scripts/profile.py --reset account_N), старый файл уходит в state/profiles/.history/.

Профиль делится на две части, обе согласованы между собой:
  * сетевая — то, что реально уходит в каждый запрос curl_cffi: impersonate (TLS),
    User-Agent, sec-ch-ua*, Accept-Language (+ navigator.platform);
  * браузерная — только Playwright: экран, viewport, DPR, ядра, память, WebGL, языки.

Генерация детерминирована от seed. Согласованность проверяет validate_profile();
несогласованный профиль не сохраняется.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

import yaml

from . import geoip
from .constants import log
from .geo import build_accept_language
from .identity import STATE_DIR, write_json_atomic

PRESETS_PATH = Path(__file__).with_name("profile_presets.yaml")
SCHEMA_VERSION = 1

GEO_FIELDS = geoip.GEO_FIELDS
# Поля, которые не входят в «отпечаток устройства» (для проверки уникальности по парку)
_IDENTITY_FIELDS = ("account", "seed", "created_at", "source", "schema") + GEO_FIELDS

REQUIRED_FIELDS = (
    "account", "seed", "created_at", "impersonate", "user_agent", "accept_language", "platform", "os",
    "country", "locale", "timezone_id", "languages", "screen", "viewport", "device_pixel_ratio",
    "color_depth", "hardware_concurrency", "webgl_vendor", "webgl_renderer", "engine",
    "fonts_preset",
)

_CHROME_MEMORY_BUCKETS = (0.25, 0.5, 1, 2, 4, 8)


class ProfileError(Exception):
    """Профиль несогласован / не найден / нельзя перезаписать."""


class InvalidProfile(ProfileError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class ProfileExists(ProfileError):
    pass


# ---------------------------------------------------------------- #
#  Пресеты
# ---------------------------------------------------------------- #

_presets_cache: dict[str, dict] = {}


def load_presets(path: Path | str | None = None) -> dict:
    p = str(path or PRESETS_PATH)
    if p not in _presets_cache:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for key in ("platform_weights", "browser_weights", "targets", "platforms", "webgl", "tiers"):
            if key not in data:
                raise ProfileError(f"{p}: нет секции {key!r}")
        _presets_cache[p] = data
    return _presets_cache[p]


def available_targets(presets: dict | None = None) -> set[str] | None:
    """impersonate-цели, которые знает установленный curl_cffi (None — curl_cffi не стоит,
    не фильтруем). Профиль с целью, которой нет в контейнере, воркер не сможет применить."""
    try:
        from curl_cffi.requests import BrowserType
        return {b.value for b in BrowserType}
    except Exception:
        return None


# ---------------------------------------------------------------- #
#  Разбор impersonate-цели
# ---------------------------------------------------------------- #

_CHROME_RE = re.compile(r"^chrome(\d+)(a)?(_android)?$")
_FIREFOX_RE = re.compile(r"^firefox(\d+)$")
_SAFARI_RE = re.compile(r"^safari(\d+)(_ios)?$")


def parse_target(impersonate: str | None) -> dict:
    """'chrome131_android' -> {family:'chrome', version:'131', mobile:'android'};
    'safari260_ios' -> {family:'safari', digits:'260', mobile:'ios'}. ValueError — неподдерживаемо."""
    imp = (impersonate or "").strip().lower()
    m = _CHROME_RE.match(imp)
    if m:
        return {"family": "chrome", "version": m.group(1), "mobile": "android" if m.group(3) else None, "id": imp}
    m = _FIREFOX_RE.match(imp)
    if m:
        return {"family": "firefox", "version": m.group(1), "mobile": None, "id": imp}
    m = _SAFARI_RE.match(imp)
    if m:
        return {"family": "safari", "digits": m.group(1), "mobile": "ios" if m.group(2) else None, "id": imp}
    raise ValueError(f"неподдерживаемый impersonate: {impersonate!r}")


def _safari_version(target: dict, presets: dict) -> str:
    imp = target["id"]
    if target["mobile"] == "ios":
        row = presets.get("ios_versions", {}).get(imp)
        if row:
            return row[1]
    base = imp.replace("_ios", "")
    if base in presets.get("safari_versions", {}):
        return presets["safari_versions"][base]
    d = target["digits"]
    return f"{d[:2]}.{d[2]}" + (f".{d[3]}" if len(d) > 3 else "")


def _ios_os_string(target: dict, presets: dict) -> str:
    row = presets.get("ios_versions", {}).get(target["id"])
    if row:
        return row[0]
    return _safari_version(target, presets).replace(".", "_")


def engine_for(family: str, mobile: str | None = None) -> str:
    return {"chrome": "chromium", "firefox": "firefox", "safari": "webkit"}[family]


# ---------------------------------------------------------------- #
#  UA / sec-ch-ua
# ---------------------------------------------------------------- #

def build_user_agent(os_name: str, target: dict, presets: dict) -> str:
    plat = presets["platforms"][os_name]
    fam = target["family"]
    if fam == "chrome":
        m = target["version"]
        if os_name == "android":
            return (f"Mozilla/5.0 ({plat['ua_token']}) AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{m}.0.0.0 Mobile Safari/537.36")
        return f"Mozilla/5.0 ({plat['ua_token']}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{m}.0.0.0 Safari/537.36"
    if fam == "firefox":
        m = target["version"]
        token = plat.get("ua_token_firefox") or plat["ua_token"]
        return f"Mozilla/5.0 ({token}; rv:{m}.0) Gecko/20100101 Firefox/{m}.0"
    ver = _safari_version(target, presets)
    if os_name == "ios":
        return (f"Mozilla/5.0 (iPhone; CPU iPhone OS {_ios_os_string(target, presets)} like Mac OS X) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{ver} Mobile/15E148 Safari/604.1")
    return f"Mozilla/5.0 ({plat['ua_token']}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{ver} Safari/605.1.15"


def build_sec_ch_ua(major: int | str) -> str:
    """Список брендов как у настоящего Chrome: «GREASE»-бренд и порядок зависят от major
    (алгоритм Chromium; сверено с Chrome 120/131/136/142)."""
    seed = int(major)
    chars = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
    grease = f"Not{chars[seed % 11]}A{chars[(seed + 1) % 11]}Brand"
    grease_ver = ("8", "99", "24")[seed % 3]
    orders = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))
    order = orders[seed % 6]
    brands: list[tuple[str, str] | None] = [None, None, None]
    brands[order[0]] = (grease, grease_ver)
    brands[order[1]] = ("Chromium", str(seed))
    brands[order[2]] = ("Google Chrome", str(seed))
    return ", ".join(f'"{n}";v="{v}"' for n, v in brands)  # type: ignore[misc]


# ---------------------------------------------------------------- #
#  Выбор из таблиц (детерминированно от rng)
# ---------------------------------------------------------------- #

def _wchoice(rng: random.Random, items: list[dict], key: str = "wt") -> dict:
    total = sum(float(i.get(key, 1)) for i in items)
    x = rng.random() * total
    acc = 0.0
    for it in items:
        acc += float(it.get(key, 1))
        if x <= acc:
            return it
    return items[-1]


def _wdict(rng: random.Random, weights: dict[str, float]) -> str:
    return _wchoice(rng, [{"k": k, "wt": v} for k, v in weights.items()])["k"]


def _memory_bucket(ram_gb: float) -> float:
    best = _CHROME_MEMORY_BUCKETS[0]
    for b in _CHROME_MEMORY_BUCKETS:
        if ram_gb >= b:
            best = b
    return best


def _pick_target(rng: random.Random, presets: dict, family_key: str, avail: set[str] | None) -> str:
    rows = presets["targets"][family_key]
    if avail:
        filtered = [r for r in rows if r["id"] in avail]
        rows = filtered or rows
    return _wchoice(rng, rows)["id"]


def derive_seed(base: int | str, name: str, attempt: int = 0) -> int:
    h = hashlib.sha256(f"{base}:{name}:{attempt}".encode()).digest()
    return int.from_bytes(h[:8], "big") >> 1


# ---------------------------------------------------------------- #
#  Генератор
# ---------------------------------------------------------------- #

def generate_profile(name: str, seed: int, *, geo: dict | None = None, os_name: str | None = None,
                     impersonate: str | None = None, user_agent: str | None = None,
                     presets: dict | None = None, created_at: float | None = None,
                     source: str = "generated") -> dict:
    """Профиль аккаунта. Одинаковые (name, seed, os_name, impersonate, presets) -> одинаковый результат.

    geo         — готовые гео-поля (geoip.resolve_geo / geo_for_country); без них — en-US c пометкой.
    os_name     — зафиксировать платформу (миграция); иначе по platform_weights.
    impersonate — зафиксировать TLS-цель (миграция); иначе из пресетов подходящего семейства.
    user_agent  — оставить прежний UA (миграция «без смены отпечатка»), остальное — из пресета.
    """
    presets = presets or load_presets()
    rng = random.Random(seed)
    avail = available_targets(presets)

    # 1. платформа и TLS-цель
    if impersonate:
        target = parse_target(impersonate)
        if os_name is None:
            os_name = ("android" if target["mobile"] == "android" else "ios" if target["mobile"] == "ios"
                       else _wdict(rng, {k: v for k, v in presets["platform_weights"].items()
                                         if target["family"] in presets["browser_weights"].get(k, {})
                                         and not presets["platforms"][k].get("mobile")}))
    else:
        os_name = os_name or _wdict(rng, presets["platform_weights"])
        fam = _wdict(rng, presets["browser_weights"][os_name])
        key = {"android": "chrome_android", "ios": "safari_ios"}.get(os_name, fam)
        impersonate = _pick_target(rng, presets, key, avail)
        target = parse_target(impersonate)
    plat = presets["platforms"][os_name]
    fam = target["family"]
    if os_name not in presets["webgl"] or fam not in presets["webgl"][os_name]:
        raise ProfileError(f"нет WebGL-шаблона для {os_name}/{fam}")

    # 2. экран / железо
    mobile = bool(plat.get("mobile"))
    gpu_vendor_name = gpu_name = chip = None
    if mobile:
        dev = _wchoice(rng, plat["devices"])
        sw, sh, dpr = dev["w"], dev["h"], dev["dpr"]
        cores = dev["cores"]
        ram = float(dev.get("ram", 4))
        gpu_vendor_name, gpu_name = dev.get("vendor_name"), dev.get("name")
        device_model = dev["model"]
    else:
        scr = _wchoice(rng, plat["screens"])
        sw, sh, dpr = scr["w"], scr["h"], scr["dpr"]
        gpu = _wchoice(rng, plat["gpus"])
        gpu_vendor_name, gpu_name, chip = gpu.get("vendor_name"), gpu.get("name"), gpu.get("chip")
        tier = presets["tiers"][gpu.get("tier", "mid")]
        cores = gpu.get("cores") or rng.choice(tier["cores"])
        ram = float(rng.choice(gpu.get("ram") or tier["ram"]))
        device_model = None
    tpl = presets["webgl"][os_name][fam]
    fmt = {"vendor_name": gpu_vendor_name or "", "name": gpu_name or "", "chip": chip or ""}
    webgl_vendor, webgl_renderer = tpl["vendor"].format(**fmt), tpl["renderer"].format(**fmt)

    # 3. сеть
    engine = engine_for(fam)
    ua = user_agent or build_user_agent(os_name, target, presets)
    chromium = engine == "chromium"
    geo = geo or geoip.geo_for_country(None)
    locale = geo["locale"]

    profile = {
        "schema": SCHEMA_VERSION,
        "account": name,
        "seed": seed,
        "created_at": int(created_at if created_at is not None else time.time()),
        "source": source,
        # --- сеть
        "impersonate": impersonate,
        "user_agent": ua,
        "sec_ch_ua": build_sec_ch_ua(target["version"]) if chromium else None,
        "sec_ch_ua_mobile": ("?1" if os_name == "android" else "?0") if chromium else None,
        "sec_ch_ua_platform": f'"{plat["sec_ch_platform"]}"' if chromium else None,
        "accept_language": build_accept_language(locale, impersonate),
        "platform": plat["navigator_platform"],
        "os": os_name,
        # --- гео
        **{k: copy.deepcopy(geo[k]) for k in GEO_FIELDS if k in geo},
        # --- экран
        "screen": {"w": sw, "h": sh},
        "viewport": {"w": sw, "h": max(300, sh - int(plat.get("ui_height", 100)))},
        "device_pixel_ratio": dpr,
        "color_depth": plat.get("color_depth", 24),
        # --- железо
        "hardware_concurrency": int(cores),
        "ram_gb": ram,
        "device_memory_gb": _memory_bucket(ram) if chromium else None,
        "webgl_vendor": webgl_vendor,
        "webgl_renderer": webgl_renderer,
        # --- прочее
        "engine": engine,
        "channel": None,
        "device": presets.get("playwright_devices", {}).get(os_name) if mobile else None,
        "mobile": mobile,
        "device_model": device_model,
        "fonts_preset": plat["fonts_preset"],
    }
    for k in GEO_FIELDS:
        profile.setdefault(k, None)
    return profile


def fingerprint(profile: dict) -> str:
    """Хэш «устройства» (без account/seed/времени и без гео): два аккаунта с одним хэшем —
    полностью одинаковые устройства."""
    core = {k: v for k, v in profile.items() if k not in _IDENTITY_FIELDS and k != "accept_language"}
    return hashlib.sha1(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def generate_fleet(names: list[str], base_seed: int | str = 0, geos: dict[str, dict] | None = None,
                   presets: dict | None = None, created_at: float | None = None,
                   taken: set[str] | None = None) -> dict[str, dict]:
    """Профили на весь парк: детерминированно от base_seed, без двух одинаковых устройств
    (при совпадении seed сдвигается). taken — отпечатки уже существующих профилей."""
    presets = presets or load_presets()
    seen = set(taken or ())
    out: dict[str, dict] = {}
    for name in names:
        geo = (geos or {}).get(name)
        for attempt in range(200):
            p = generate_profile(name, derive_seed(base_seed, name, attempt), geo=geo,
                                 presets=presets, created_at=created_at)
            fp = fingerprint(p)
            if fp not in seen:
                seen.add(fp)
                out[name] = p
                break
        else:  # практически недостижимо при таблицах из пресетов
            raise ProfileError(f"{name}: не удалось подобрать уникальное устройство за 200 попыток")
    return out


# ---------------------------------------------------------------- #
#  Миграция из accounts.yaml (без смены отпечатка)
# ---------------------------------------------------------------- #

def os_from_user_agent(ua: str | None) -> str | None:
    u = ua or ""
    if "iPhone" in u or "iPad" in u:
        return "ios"
    if "Android" in u:
        return "android"
    if "Windows NT" in u:
        return "windows"
    if "Macintosh" in u:
        return "macos"
    if "X11" in u or "Linux" in u:
        return "linux"
    return None


def profile_from_account(acc: dict, geo: dict, presets: dict | None = None,
                         created_at: float | None = None) -> dict:
    """Профиль для существующего аккаунта: impersonate, user_agent и locale берутся из
    accounts.yaml КАК ЕСТЬ (отпечаток для Reddit не меняется), недостающее (экран, железо,
    WebGL) подбирается из пресета той же платформы. Seed стабилен: от имени аккаунта."""
    name = acc["name"]
    imp, ua = acc.get("impersonate"), acc.get("user_agent")
    if not imp or not ua:
        raise ProfileError(f"{name}: в accounts.yaml нужны impersonate и user_agent")
    os_name = os_from_user_agent(ua)
    if os_name is None:
        raise ProfileError(f"{name}: не могу определить платформу по UA {ua!r}")
    return generate_profile(name, derive_seed("migrate", name), geo=geo, os_name=os_name,
                            impersonate=imp, user_agent=ua, presets=presets,
                            created_at=created_at, source="accounts.yaml")


# ---------------------------------------------------------------- #
#  validate_profile
# ---------------------------------------------------------------- #

_OS_FROM_UA_FAMILY = {"chrome": "chromium", "firefox": "firefox", "safari": "webkit"}


def _ua_browser(ua: str) -> str | None:
    if "Firefox/" in ua:
        return "firefox"
    if "Chrome/" in ua or "Chromium/" in ua:
        return "chrome"
    if "Safari/" in ua and "Version/" in ua:
        return "safari"
    return None


def validate_profile(p: dict, presets: dict | None = None) -> list[str]:
    """Список несогласованностей (пусто = профиль годен): UA <-> platform <-> GPU <-> экран <->
    TLS-отпечаток <-> гео/язык."""
    presets = presets or load_presets()
    errs: list[str] = []
    missing = [k for k in REQUIRED_FIELDS if p.get(k) in (None, "")]
    if missing:
        return [f"нет полей: {', '.join(missing)}"]

    imp, ua, os_name = p["impersonate"], p["user_agent"], p["os"]
    try:
        target = parse_target(imp)
    except ValueError as e:
        return [str(e)]
    fam = target["family"]
    plat = presets["platforms"].get(os_name)
    if plat is None:
        return [f"неизвестная платформа os={os_name!r}"]

    # --- TLS <-> engine <-> UA
    if p["engine"] != _OS_FROM_UA_FAMILY[fam]:
        errs.append(f"engine={p['engine']} не подходит к impersonate={imp} (нужен {_OS_FROM_UA_FAMILY[fam]})")
    ua_family = _ua_browser(ua)
    if ua_family != fam:
        errs.append(f"UA ({ua_family}) не соответствует TLS-цели {imp} ({fam})")
    else:
        if fam == "chrome":
            m = re.search(r"Chrome/(\d+)", ua)
            if not m or m.group(1) != target["version"]:
                errs.append(f"версия Chrome в UA не равна {target['version']} из {imp}")
        elif fam == "firefox":
            rv, ff = re.search(r"rv:(\d+)", ua), re.search(r"Firefox/(\d+)", ua)
            if not (rv and ff and rv.group(1) == ff.group(1) == target["version"]):
                errs.append(f"версия Firefox в UA не равна {target['version']} из {imp}")
        else:
            m = re.search(r"Version/([\d.]+)", ua)
            if not m or m.group(1) != _safari_version(target, presets):
                errs.append(f"версия Safari в UA не равна {_safari_version(target, presets)} из {imp}")

    # --- UA <-> platform
    ua_os = os_from_user_agent(ua)
    if ua_os != os_name:
        errs.append(f"UA говорит про {ua_os}, а профиль — {os_name}")
    if p["platform"] != plat["navigator_platform"]:
        errs.append(f"navigator.platform={p['platform']!r}, для {os_name} нужен {plat['navigator_platform']!r}")
    mob_target = target["mobile"]
    if (mob_target or None) != ({"android": "android", "ios": "ios"}.get(os_name)):
        errs.append(f"мобильность TLS-цели {imp} не совпадает с платформой {os_name}")
    if os_name == "macos" and fam == "safari" and "Macintosh" not in ua:
        errs.append("Safari на macOS без Macintosh в UA")
    if fam == "safari" and os_name not in ("macos", "ios"):
        errs.append("Safari бывает только на macOS/iOS")
    if os_name == "ios" and fam != "safari":
        errs.append("на iOS — только Safari-отпечаток")
    if os_name == "android" and fam != "chrome":
        errs.append("на Android — только Chrome-отпечаток")

    # --- sec-ch-ua*: только у Chromium
    chromium = p["engine"] == "chromium"
    ch = (p.get("sec_ch_ua"), p.get("sec_ch_ua_mobile"), p.get("sec_ch_ua_platform"))
    if chromium:
        if not all(ch):
            errs.append("у Chromium должны быть sec_ch_ua / sec_ch_ua_mobile / sec_ch_ua_platform")
        else:
            if f'v="{target["version"]}"' not in ch[0] or "Chromium" not in ch[0]:
                errs.append(f"sec_ch_ua не соответствует версии {target['version']}")
            if ch[1] != ("?1" if os_name == "android" else "?0"):
                errs.append(f"sec_ch_ua_mobile={ch[1]} не соответствует платформе {os_name}")
            if ch[2] != f'"{plat["sec_ch_platform"]}"':
                errs.append(f"sec_ch_ua_platform={ch[2]} не соответствует платформе {os_name}")
    elif any(ch):
        errs.append("Firefox/Safari не шлют sec-ch-ua*: поля должны быть null")

    # --- GPU <-> платформа <-> браузер
    v, r = str(p["webgl_vendor"]), str(p["webgl_renderer"])
    if os_name == "windows":
        if "Direct3D" not in r:
            errs.append(f"WebGL-рендерер {r!r} не похож на Windows (нет Direct3D)")
    elif os_name == "macos":
        if fam == "safari":
            if (v, r) != ("Apple Inc.", "Apple GPU"):
                errs.append("Safari на macOS отдаёт WebGL Apple Inc./Apple GPU")
        elif "Apple" not in r:
            errs.append(f"WebGL-рендерер {r!r} не похож на Apple GPU для macOS")
    elif os_name == "linux":
        if not re.search(r"Mesa|NVIDIA|AMD|Radeon|OpenGL", r):
            errs.append(f"WebGL-рендерер {r!r} не похож на Linux")
        if "Direct3D" in r or "Metal" in r:
            errs.append("Linux-профиль с Direct3D/Metal рендерером")
    elif os_name == "android":
        if not re.search(r"Adreno|Mali|PowerVR", r):
            errs.append(f"WebGL-рендерер {r!r} не похож на мобильный Android GPU")
    elif os_name == "ios":
        if (v, r) != ("Apple Inc.", "Apple GPU"):
            errs.append("Safari на iOS отдаёт WebGL Apple Inc./Apple GPU")
    if v.startswith("Google Inc. ("):
        inner = v[len("Google Inc. ("):-1]
        if not r.startswith(f"ANGLE ({inner},"):
            errs.append(f"WebGL vendor {v!r} и renderer {r!r} противоречат друг другу")

    # --- экран
    s, vp, dpr = p["screen"], p["viewport"], float(p["device_pixel_ratio"])
    try:
        sw, sh, vw, vh = int(s["w"]), int(s["h"]), int(vp["w"]), int(vp["h"])
    except (KeyError, TypeError, ValueError):
        return errs + ["screen/viewport должны быть {w,h}"]
    if vw > sw or vh > sh:
        errs.append("viewport больше экрана")
    if plat.get("mobile"):
        if not (sw <= 500 and sh > sw):
            errs.append(f"мобильный профиль с экраном {sw}x{sh}")
        if dpr < 2:
            errs.append(f"мобильный профиль с DPR={dpr}")
        if not p.get("mobile"):
            errs.append("мобильная платформа, но mobile=false")
    else:
        if sw < 1280 or sh < 720:
            errs.append(f"десктопный профиль с экраном {sw}x{sh}")
        if not (1 <= dpr <= 3):
            errs.append(f"десктопный DPR={dpr} вне 1..3")
        if p.get("mobile"):
            errs.append("десктопная платформа, но mobile=true")
        if os_name == "windows" and dpr > 2:
            errs.append(f"Windows с DPR={dpr}")
    if int(p["color_depth"]) not in (24, 30, 32):
        errs.append(f"color_depth={p['color_depth']}")

    # --- железо
    cores = p["hardware_concurrency"]
    if not isinstance(cores, int) or not (2 <= cores <= 32):
        errs.append(f"hardware_concurrency={cores!r}")
    elif plat.get("mobile") and cores < 4:
        errs.append("мобильный профиль с <4 ядрами")
    dm = p.get("device_memory_gb")
    if chromium:
        if dm not in _CHROME_MEMORY_BUCKETS:
            errs.append(f"device_memory_gb={dm!r}: у Chrome значения только {_CHROME_MEMORY_BUCKETS} (максимум 8)")
    elif dm is not None:
        errs.append("navigator.deviceMemory нет у Firefox/Safari: device_memory_gb должен быть null")
    ram = p.get("ram_gb")
    if ram is not None and not (2 <= float(ram) <= 64):
        errs.append(f"ram_gb={ram!r}")

    # --- гео / язык
    errs += validate_geo(p)
    return errs


def validate_geo(p: dict) -> list[str]:
    errs: list[str] = []
    loc, langs = p.get("locale"), p.get("languages")
    if not isinstance(langs, list) or not langs or langs[0] != loc:
        errs.append("languages[0] должен совпадать с locale")
    if not geoip.valid_timezone(p.get("timezone_id")):
        errs.append(f"timezone_id={p.get('timezone_id')!r} не IANA-пояс")
    try:
        expected = build_accept_language(loc, p.get("impersonate"))
    except Exception:
        expected = None
    if expected and p.get("accept_language") != expected:
        errs.append(f"accept_language={p.get('accept_language')!r}, для {loc}/{p.get('impersonate')} нужен {expected!r}")
    if p.get("geo_known") is False:
        if loc != geoip.DEFAULT_LOCALE:
            errs.append("неизвестная страна: locale обязан быть en-US")
        if not p.get("geo_note"):
            errs.append("неизвестная страна без geo_note (нужна пометка для панели)")
    else:
        cc = p.get("country")
        row = geoip.COUNTRY_GEO.get(cc) if cc else None
        if row is None:
            errs.append(f"country={cc!r} нет в COUNTRY_GEO, а geo_known не false")
        else:
            region = str(loc).split("-")[-1].upper()
            if region != cc and not (cc == "GB" and region == "GB"):
                errs.append(f"locale {loc} не соответствует стране {cc}")
    return errs


def assert_valid(profile: dict, presets: dict | None = None) -> None:
    errs = validate_profile(profile, presets)
    if errs:
        raise InvalidProfile(errs)


# ---------------------------------------------------------------- #
#  Хранилище: state/profiles/account_N.json (read-only после создания)
# ---------------------------------------------------------------- #

def profiles_dir(state_dir: Path | str | None = None) -> Path:
    return Path(state_dir or STATE_DIR) / "profiles"


def profile_path(name: str, state_dir: Path | str | None = None) -> Path:
    return profiles_dir(state_dir) / f"{name}.json"


def load_profile(name: str, state_dir: Path | str | None = None) -> dict | None:
    p = profile_path(name, state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("профиль %s нечитаем: %s", p, e)
        return None
    return data if isinstance(data, dict) and data.get("impersonate") else None


def _write_readonly(path: Path, data: dict) -> None:
    write_json_atomic(path, data)
    try:
        os.chmod(path, 0o444)   # защита от случайной правки руками; замена — только через os.replace
    except OSError:
        pass


def create_profile(profile: dict, state_dir: Path | str | None = None, presets: dict | None = None) -> Path:
    """Сохранить НОВЫЙ профиль. Существующий не перезаписывается (ProfileExists) —
    для замены есть reset_profile(). Несогласованный профиль не сохраняется (InvalidProfile)."""
    assert_valid(profile, presets)
    path = profile_path(profile["account"], state_dir)
    if path.exists():
        raise ProfileExists(f"{path} уже есть: профиль постоянный, замена — scripts/profile.py --reset {profile['account']}")
    _write_readonly(path, profile)
    return path


def reset_profile(name: str, new_profile: dict, state_dir: Path | str | None = None,
                  presets: dict | None = None) -> Path:
    """Явная замена профиля. Старый уходит в state/profiles/.history/<name>.<ts>.json."""
    if new_profile.get("account") != name:
        raise ProfileError(f"профиль для {new_profile.get('account')!r}, а сбрасывается {name!r}")
    assert_valid(new_profile, presets)
    path = profile_path(name, state_dir)
    if path.exists():
        hist = path.parent / ".history" / f"{name}.{int(time.time())}.json"
        write_json_atomic(hist, json.loads(path.read_text(encoding="utf-8")))
    _write_readonly(path, new_profile)
    return path


def update_geo(name: str, geo: dict, state_dir: Path | str | None = None,
               presets: dict | None = None) -> dict:
    """Единственная допустимая правка существующего профиля: гео-группа (смена ноды/IP).
    Всё остальное остаётся байт-в-байт; accept_language пересчитывается из нового locale."""
    cur = load_profile(name, state_dir)
    if cur is None:
        raise ProfileError(f"{name}: профиля нет")
    new = dict(cur)
    for k in GEO_FIELDS:
        if k in geo:
            new[k] = copy.deepcopy(geo[k])
    new["accept_language"] = build_accept_language(new["locale"], new["impersonate"])
    assert_valid(new, presets)
    _write_readonly(profile_path(name, state_dir), new)
    return new


def refresh_geo(name: str, proxy_url: str | None, fetch=None, state_dir: Path | str | None = None,
                presets: dict | None = None, force: bool = False) -> dict | None:
    """Перепроверка гео через прокси слота (после смены ноды). Возвращает обновлённый профиль,
    если гео поменялось, иначе None. Сеть недоступна -> профиль не трогаем (исключение наружу)."""
    cur = load_profile(name, state_dir)
    if cur is None:
        raise ProfileError(f"{name}: профиля нет")
    geo = geoip.resolve_geo(proxy_url, fetch=fetch)
    if not force and all(cur.get(k) == geo.get(k) for k in ("country", "locale", "timezone_id", "exit_ip")):
        return None
    return update_geo(name, geo, state_dir, presets)


# ---------------------------------------------------------------- #
#  Применение профиля: ОДНИ И ТЕ ЖЕ UA/язык и для скрапера, и для браузера
# ---------------------------------------------------------------- #

def http_headers(profile: dict) -> dict[str, str]:
    """Заголовки для каждого запроса curl_cffi (поверх тех, что ставит impersonate)."""
    h = {"User-Agent": profile["user_agent"], "Accept-Language": profile["accept_language"]}
    for key, hdr in (("sec_ch_ua", "sec-ch-ua"), ("sec_ch_ua_mobile", "sec-ch-ua-mobile"),
                     ("sec_ch_ua_platform", "sec-ch-ua-platform")):
        if profile.get(key):
            h[hdr] = profile[key]
    return h


def playwright_context_kwargs(profile: dict) -> dict:
    """Аргументы browser.new_context(): viewport/screen/locale/timezone_id/user_agent + те же
    Accept-Language, что у скрапера (Playwright по умолчанию шлёт только bare-locale)."""
    kw: dict = {
        "user_agent": profile["user_agent"],
        "locale": profile["locale"],
        "timezone_id": profile["timezone_id"],
        "viewport": {"width": profile["viewport"]["w"], "height": profile["viewport"]["h"]},
        "screen": {"width": profile["screen"]["w"], "height": profile["screen"]["h"]},
        "device_scale_factor": float(profile["device_pixel_ratio"]),
        "extra_http_headers": {"Accept-Language": profile["accept_language"]},
    }
    if profile.get("mobile") and profile.get("engine") != "firefox":
        kw["is_mobile"] = True
        kw["has_touch"] = True
    return kw


def init_script(profile: dict) -> str | None:
    """JS для context.add_init_script(): hardwareConcurrency, deviceMemory (только Chromium),
    languages и WebGL vendor/renderer. Ничего больше не подменяется."""
    parts = []
    if profile.get("hardware_concurrency"):
        parts.append(f"Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{int(profile['hardware_concurrency'])}}});")
    if profile.get("device_memory_gb") is not None:
        parts.append(f"Object.defineProperty(navigator,'deviceMemory',{{get:()=>{float(profile['device_memory_gb'])}}});")
    if profile.get("languages"):
        parts.append(f"Object.defineProperty(navigator,'languages',{{get:()=>Object.freeze({json.dumps(profile['languages'])})}});")
    if profile.get("webgl_vendor") and profile.get("webgl_renderer"):
        v, r = json.dumps(profile["webgl_vendor"]), json.dumps(profile["webgl_renderer"])
        parts.append(
            "for (const C of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) { if (!C) continue;"
            "const g = C.prototype.getParameter;"
            f"C.prototype.getParameter = function(p) {{ if (p === 37445) return {v}; if (p === 37446) return {r}; "
            "return g.call(this, p); }; }")
    return "\n".join(parts) or None
