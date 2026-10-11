"""
Accept-Language под гео ноды слота.

Раньше curl_cffi-запросы шли с дефолтным "en-US,en;q=0.9", тогда как
браузер при харвесте cookies открывался с локалью ноды (sr-RS, ja-JP...).
Рассинхрон "IP одной страны, язык другой" — лишний признак для антибота.

Формат заголовка зависит от семейства браузера (по impersonate):
  chrome / edge : "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7"
  firefox       : "sr-RS,sr;q=0.8,en-US;q=0.5,en;q=0.3"
  safari        : "sr-RS,sr;q=0.9"
Для английских локалей (en-*) хвост "en" не дублируется.
"""

import re

DEFAULT_LOCALE = "en-US"

_LOCALE_RE = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")


def normalize_locale(locale: str | None) -> str:
    """'sr_RS' -> 'sr-RS'; мусор/пусто -> DEFAULT_LOCALE."""
    if not locale or not isinstance(locale, str):
        return DEFAULT_LOCALE
    loc = locale.strip().replace("_", "-")
    if not _LOCALE_RE.match(loc):
        return DEFAULT_LOCALE
    return loc


def _family(impersonate: str | None) -> str:
    imp = (impersonate or "").lower()
    if imp.startswith("firefox"):
        return "firefox"
    if imp.startswith("safari"):
        return "safari"
    return "chrome"  # chrome*, edge*, неизвестное


def build_accept_language(locale: str | None, impersonate: str | None = None) -> str:
    loc = normalize_locale(locale)
    lang = loc.split("-", 1)[0].lower()
    family = _family(impersonate)

    # (q для "lang", q для "en-US", q для "en") по семейству
    q_lang, q_enus, q_en = {
        "chrome": (0.9, 0.8, 0.7),
        "firefox": (0.8, 0.5, 0.3),
        "safari": (0.9, None, None),
    }[family]

    if lang == "en":
        # en-US -> "en-US,en;q=0.9"; en-SG -> "en-SG,en;q=0.9" (+ en-US у chrome/firefox)
        parts = [loc]
        if loc != "en":
            parts.append(f"en;q={q_lang}")
        return ",".join(parts)

    parts = [loc]
    if lang != loc.lower():
        parts.append(f"{lang};q={q_lang}")
    if q_enus is not None:
        parts.append(f"en-US;q={q_enus}")
        parts.append(f"en;q={q_en}")
    return ",".join(parts)


def pick_locale(profile: dict | None, account: dict | None) -> str:
    """Приоритет: активный профиль слота после ротации (state/slots,
    geo.locale) -> поле locale в accounts.yaml -> en-US."""
    if profile:
        loc = (profile.get("geo") or {}).get("locale")
        if loc:
            return normalize_locale(loc)
    if account and account.get("locale"):
        return normalize_locale(account["locale"])
    return DEFAULT_LOCALE
