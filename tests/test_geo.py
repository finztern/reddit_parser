import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.geo import DEFAULT_LOCALE, build_accept_language, normalize_locale, pick_locale


def test_chrome_non_english():
    assert build_accept_language("sr-RS", "chrome150") == "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7"


def test_firefox_non_english():
    assert build_accept_language("ja-JP", "firefox147") == "ja-JP,ja;q=0.8,en-US;q=0.5,en;q=0.3"


def test_safari_non_english():
    assert build_accept_language("hu-HU", "safari260") == "hu-HU,hu;q=0.9"


def test_english_locales_no_duplicate_tail():
    assert build_accept_language("en-US", "chrome131") == "en-US,en;q=0.9"
    assert build_accept_language("en-SG", "firefox144") == "en-SG,en;q=0.8"


def test_underscore_and_garbage():
    assert normalize_locale("sr_RS") == "sr-RS"
    assert normalize_locale("") == DEFAULT_LOCALE
    assert normalize_locale(None) == DEFAULT_LOCALE
    assert normalize_locale("!!bad") == DEFAULT_LOCALE


def test_unknown_impersonate_falls_back_to_chrome_format():
    assert build_accept_language("de-DE", None).startswith("de-DE,de;q=0.9")


def test_pick_locale_priority():
    prof = {"geo": {"locale": "fr-FR"}}
    acc = {"locale": "de-DE"}
    assert pick_locale(prof, acc) == "fr-FR"      # профиль после ротации главнее
    assert pick_locale(None, acc) == "de-DE"      # затем accounts.yaml
    assert pick_locale({"geo": {}}, acc) == "de-DE"
    assert pick_locale(None, {}) == DEFAULT_LOCALE
