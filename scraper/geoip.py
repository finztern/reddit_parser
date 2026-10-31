"""
Гео из выходного IP ноды (модуль 03).

Locale, часовой пояс и языки профиля НЕ пишутся руками: определяется выходной IP
**через прокси слота** (не IP сервера!), по нему — страна, а из страны — locale/languages.
Часовой пояс берём у сервиса, если он вернул валидный IANA-пояс (у США/Канады/Австралии/
Бразилии/Мексики/России пояса разные), иначе — основной пояс страны из COUNTRY_GEO.

Источники (по очереди, первый ответивший):
  1. локальная MaxMind-база — если задан GEOIP_MMDB (путь к GeoLite2-City/Country.mmdb) и
     установлен пакет geoip2; сам IP узнаётся через ipify (тоже через прокси);
  2. ipinfo.io/json;
  3. ip-api.com/json.

Неизвестная страна -> en-US + явная пометка (geo_known=False, geo_note) — «молчаливого дефолта»
нет: пометку показывает панель (модуль 07) и CLI scripts/profile.py.

Сеть вынесена в параметр `fetch(url, proxy_url, timeout) -> str`, поэтому тесты идут без сети.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import log

DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE = "America/New_York"
GEO_TTL_SECONDS = 24 * 3600

Fetch = Callable[[str, "str | None", float], str]

# ISO-3166 alpha-2 (верхний регистр) -> (locale, основной timezone).
# Расширена до всех стран, которые встречаются в нодах парка (+ запас). Не хватает страны —
# дописываешь строку сюда, остальное считается само.
COUNTRY_GEO: dict[str, tuple[str, str]] = {
    "US": ("en-US", "America/New_York"), "CA": ("en-CA", "America/Toronto"),
    "GB": ("en-GB", "Europe/London"), "IE": ("en-IE", "Europe/Dublin"),
    "DE": ("de-DE", "Europe/Berlin"), "FR": ("fr-FR", "Europe/Paris"),
    "NL": ("nl-NL", "Europe/Amsterdam"), "BE": ("nl-BE", "Europe/Brussels"),
    "LU": ("fr-LU", "Europe/Luxembourg"), "CH": ("de-CH", "Europe/Zurich"),
    "AT": ("de-AT", "Europe/Vienna"), "SE": ("sv-SE", "Europe/Stockholm"),
    "NO": ("nb-NO", "Europe/Oslo"), "DK": ("da-DK", "Europe/Copenhagen"),
    "FI": ("fi-FI", "Europe/Helsinki"), "IS": ("is-IS", "Atlantic/Reykjavik"),
    "PL": ("pl-PL", "Europe/Warsaw"), "CZ": ("cs-CZ", "Europe/Prague"),
    "SK": ("sk-SK", "Europe/Bratislava"), "HU": ("hu-HU", "Europe/Budapest"),
    "RO": ("ro-RO", "Europe/Bucharest"), "BG": ("bg-BG", "Europe/Sofia"),
    "RS": ("sr-RS", "Europe/Belgrade"), "HR": ("hr-HR", "Europe/Zagreb"),
    "SI": ("sl-SI", "Europe/Ljubljana"), "BA": ("bs-BA", "Europe/Sarajevo"),
    "MK": ("mk-MK", "Europe/Skopje"), "AL": ("sq-AL", "Europe/Tirane"),
    "GR": ("el-GR", "Europe/Athens"), "CY": ("el-CY", "Asia/Nicosia"),
    "MT": ("en-MT", "Europe/Malta"), "TR": ("tr-TR", "Europe/Istanbul"),
    "UA": ("uk-UA", "Europe/Kiev"), "MD": ("ro-MD", "Europe/Chisinau"),
    "BY": ("be-BY", "Europe/Minsk"), "RU": ("ru-RU", "Europe/Moscow"),
    "LV": ("lv-LV", "Europe/Riga"), "LT": ("lt-LT", "Europe/Vilnius"),
    "EE": ("et-EE", "Europe/Tallinn"), "ES": ("es-ES", "Europe/Madrid"),
    "PT": ("pt-PT", "Europe/Lisbon"), "IT": ("it-IT", "Europe/Rome"),
    "GE": ("ka-GE", "Asia/Tbilisi"), "AM": ("hy-AM", "Asia/Yerevan"),
    "AZ": ("az-AZ", "Asia/Baku"), "KZ": ("kk-KZ", "Asia/Almaty"),
    "IL": ("he-IL", "Asia/Jerusalem"), "AE": ("ar-AE", "Asia/Dubai"),
    "SA": ("ar-SA", "Asia/Riyadh"), "EG": ("ar-EG", "Africa/Cairo"),
    "MA": ("fr-MA", "Africa/Casablanca"), "ZA": ("en-ZA", "Africa/Johannesburg"),
    "NG": ("en-NG", "Africa/Lagos"), "KE": ("en-KE", "Africa/Nairobi"),
    "IN": ("en-IN", "Asia/Kolkata"), "PK": ("ur-PK", "Asia/Karachi"),
    "BD": ("bn-BD", "Asia/Dhaka"), "LK": ("si-LK", "Asia/Colombo"),
    "JP": ("ja-JP", "Asia/Tokyo"), "KR": ("ko-KR", "Asia/Seoul"),
    "HK": ("zh-HK", "Asia/Hong_Kong"), "TW": ("zh-TW", "Asia/Taipei"),
    "SG": ("en-SG", "Asia/Singapore"), "MY": ("ms-MY", "Asia/Kuala_Lumpur"),
    "TH": ("th-TH", "Asia/Bangkok"), "VN": ("vi-VN", "Asia/Ho_Chi_Minh"),
    "ID": ("id-ID", "Asia/Jakarta"), "PH": ("en-PH", "Asia/Manila"),
    "AU": ("en-AU", "Australia/Sydney"), "NZ": ("en-NZ", "Pacific/Auckland"),
    "BR": ("pt-BR", "America/Sao_Paulo"), "MX": ("es-MX", "America/Mexico_City"),
    "AR": ("es-AR", "America/Argentina/Buenos_Aires"), "CL": ("es-CL", "America/Santiago"),
    "CO": ("es-CO", "America/Bogota"), "PE": ("es-PE", "America/Lima"),
}

# Как страну называют в именах нод (us74.example, uk3, ...) -> ISO alpha-2
NODE_CODE_ALIASES = {"UK": "GB"}


def normalize_country(code: str | None) -> str | None:
    if not code or not isinstance(code, str):
        return None
    cc = code.strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        return None
    return NODE_CODE_ALIASES.get(cc, cc)


def country_from_node_name(node_name: str | None) -> str | None:
    """'us74.example' -> 'US'; 'rs1' -> 'RS'. Используется как запасной вариант,
    основным источником всегда остаётся реальный выходной IP."""
    import re
    m = re.match(r"([a-zA-Z]{2})\d", (node_name or "").strip())
    return normalize_country(m.group(1)) if m else None


def languages_for(locale: str) -> list[str]:
    """['sr-RS', 'sr', 'en-US', 'en'] — как у navigator.languages на нормальной машине.
    Для английских локалей хвост не дублируется."""
    loc = (locale or DEFAULT_LOCALE).replace("_", "-")
    lang = loc.split("-", 1)[0].lower()
    if lang == "en":
        return [loc, "en"] if loc.lower() != "en" else ["en"]
    out = [loc]
    if lang != loc.lower():
        out.append(lang)
    out += ["en-US", "en"]
    return out


# Устаревшие, но рабочие в Chromium/Firefox имена, которых может не быть в системной tzdata
_LEGACY_TIMEZONES = {"Europe/Kiev"}


def valid_timezone(tz: str | None) -> bool:
    if not tz or not isinstance(tz, str):
        return False
    if tz in _LEGACY_TIMEZONES:
        return True
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def geo_for_country(country: str | None, timezone_hint: str | None = None) -> dict:
    """Страна (+ необязательный пояс от сервиса) -> набор гео-полей профиля.
    Неизвестная страна -> en-US и пометка, а не тихий дефолт."""
    cc = normalize_country(country)
    row = COUNTRY_GEO.get(cc) if cc else None
    if row is None:
        note = f"unknown_country:{cc or '??'} -> en-US (добавь страну в scraper/geoip.py:COUNTRY_GEO)"
        log.warning("geoip: %s", note)
        tz = timezone_hint if valid_timezone(timezone_hint) else DEFAULT_TIMEZONE
        return {"country": cc, "locale": DEFAULT_LOCALE, "timezone_id": tz,
                "languages": languages_for(DEFAULT_LOCALE), "geo_known": False, "geo_note": note}
    locale, tz_default = row
    tz = timezone_hint if valid_timezone(timezone_hint) else tz_default
    return {"country": cc, "locale": locale, "timezone_id": tz,
            "languages": languages_for(locale), "geo_known": True, "geo_note": None}


# ---------------------------------------------------------------- #
#  Сеть: всегда ЧЕРЕЗ прокси слота
# ---------------------------------------------------------------- #

def default_fetch(url: str, proxy_url: str | None, timeout: float = 15.0) -> str:
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0", "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _parse_ipinfo(text: str) -> dict:
    d = json.loads(text)
    if not d.get("ip") or not d.get("country"):
        raise ValueError(f"ipinfo без ip/country: {str(d)[:120]}")
    return {"ip": d["ip"], "country": d["country"], "timezone": d.get("timezone")}


def _parse_ipapi(text: str) -> dict:
    d = json.loads(text)
    if d.get("status") != "success" or not d.get("countryCode"):
        raise ValueError(f"ip-api: {str(d)[:120]}")
    return {"ip": d.get("query"), "country": d["countryCode"], "timezone": d.get("timezone")}


PROVIDERS: list[tuple[str, str, Callable[[str], dict]]] = [
    ("ipinfo", "https://ipinfo.io/json", _parse_ipinfo),
    ("ip-api", "http://ip-api.com/json/?fields=status,countryCode,timezone,query", _parse_ipapi),
]


def _maxmind_lookup(ip: str, db_path: str) -> dict | None:
    try:
        import geoip2.database  # type: ignore
    except ImportError:
        return None
    try:
        with geoip2.database.Reader(db_path) as reader:
            try:
                r = reader.city(ip)
                tz = getattr(r.location, "time_zone", None)
            except Exception:
                r = reader.country(ip)
                tz = None
            cc = r.country.iso_code
            return {"ip": ip, "country": cc, "timezone": tz} if cc else None
    except Exception as e:
        log.warning("geoip: MaxMind %s: %s", db_path, e)
        return None


def lookup_exit_ip(proxy_url: str | None, fetch: Fetch | None = None,
                   timeout: float = 15.0, mmdb: str | None = None) -> dict:
    """Выходной IP через прокси слота -> {"ip","country","timezone","source"}.
    Бросает RuntimeError, если не ответил ни один источник."""
    fetch = fetch or default_fetch
    mmdb = mmdb or os.environ.get("GEOIP_MMDB")
    errors: list[str] = []

    if mmdb and os.path.exists(mmdb):
        try:
            ip = fetch("https://api.ipify.org", proxy_url, timeout).strip()
            row = _maxmind_lookup(ip, mmdb)
            if row:
                return {**row, "source": "maxmind"}
        except Exception as e:
            errors.append(f"maxmind: {e}")

    for name, url, parse in PROVIDERS:
        try:
            row = parse(fetch(url, proxy_url, timeout))
            return {**row, "source": name}
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("не удалось определить выходной IP/страну через прокси: " + "; ".join(errors))


def resolve_geo(proxy_url: str | None, fetch: Fetch | None = None, now: float | None = None,
                mmdb: str | None = None) -> dict:
    """Полный набор гео-полей профиля: country, locale, timezone_id, languages,
    geo_source, exit_ip, checked_at (+ geo_known/geo_note)."""
    row = lookup_exit_ip(proxy_url, fetch=fetch, mmdb=mmdb)
    geo = geo_for_country(row["country"], row.get("timezone"))
    geo.update({
        "geo_source": row["source"],
        "exit_ip": row["ip"],
        "checked_at": int(now if now is not None else time.time()),
    })
    return geo


GEO_FIELDS = ("country", "locale", "timezone_id", "languages", "geo_source", "exit_ip",
              "checked_at", "geo_known", "geo_note")


def needs_recheck(profile: dict, current_exit_ip: str | None = None,
                  now: float | None = None, ttl: float = GEO_TTL_SECONDS) -> str | None:
    """Причина перепроверки гео или None. Смена выходного IP (= смена ноды) — главная причина;
    протухший кэш — вторая (пояс/страна у провайдера могли поменяться)."""
    if not profile.get("exit_ip") or not profile.get("checked_at"):
        return "no_geo"
    if current_exit_ip and current_exit_ip != profile.get("exit_ip"):
        return "exit_ip_changed"
    if (now if now is not None else time.time()) - float(profile["checked_at"]) > ttl:
        return "stale"
    return None
