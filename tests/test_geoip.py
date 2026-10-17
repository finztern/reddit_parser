import json
import sys
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import geoip  # noqa: E402


def test_country_and_node_normalization():
    assert geoip.normalize_country(" uk ") == "GB"
    assert geoip.normalize_country("ca") == "CA"
    for value in (None, "", "U", "USA", "1A", 123):
        assert geoip.normalize_country(value) is None

    assert geoip.country_from_node_name("us74.trust.zone") == "US"
    assert geoip.country_from_node_name(" uk3") == "GB"
    assert geoip.country_from_node_name("bad") is None
    assert geoip.country_from_node_name(None) is None


@pytest.mark.parametrize("locale,expected", [
    ("en-US", ["en-US", "en"]),
    ("en", ["en"]),
    ("sr_RS", ["sr-RS", "sr", "en-US", "en"]),
    ("ja-JP", ["ja-JP", "ja", "en-US", "en"]),
    ("", ["en-US", "en"]),
])
def test_languages_for_locale(locale, expected):
    assert geoip.languages_for(locale) == expected


def test_timezone_validation_and_known_country_override():
    assert geoip.valid_timezone("Europe/Kiev")
    assert geoip.valid_timezone("America/Toronto")
    assert not geoip.valid_timezone("Not/AZone")
    assert not geoip.valid_timezone(None)

    geo = geoip.geo_for_country("ca", timezone_hint="America/Vancouver")
    assert geo == {
        "country": "CA",
        "locale": "en-CA",
        "timezone_id": "America/Vancouver",
        "languages": ["en-CA", "en"],
        "geo_known": True,
        "geo_note": None,
    }


def test_unknown_country_has_explicit_fallback_and_valid_timezone(caplog):
    geo = geoip.geo_for_country("zz", timezone_hint="Europe/London")

    assert geo["country"] == "ZZ"
    assert geo["locale"] == "en-US"
    assert geo["timezone_id"] == "Europe/London"
    assert geo["geo_known"] is False
    assert "unknown_country:ZZ" in geo["geo_note"]
    assert "unknown_country:ZZ" in caplog.text


def test_provider_parsers_validate_required_fields():
    assert geoip._parse_ipinfo('{"ip":"203.0.113.1","country":"US"}') == {
        "ip": "203.0.113.1", "country": "US", "timezone": None,
    }
    assert geoip._parse_ipapi(
        '{"status":"success","countryCode":"DE","query":"203.0.113.2","timezone":"Europe/Berlin"}'
    ) == {
        "ip": "203.0.113.2", "country": "DE", "timezone": "Europe/Berlin",
    }
    with pytest.raises(ValueError):
        geoip._parse_ipinfo('{"ip":"203.0.113.1"}')
    with pytest.raises(ValueError):
        geoip._parse_ipapi('{"status":"fail","countryCode":"DE"}')


def test_default_fetch_builds_proxy_and_decodes_response(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"ok":true}'

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(geoip.urllib.request, "build_opener", build_opener)
    assert geoip.default_fetch("https://example.test", "http://proxy:1", 2.5) == '{"ok":true}'
    assert isinstance(captured["handlers"][0], geoip.urllib.request.ProxyHandler)
    assert captured["request"].get_header("User-agent") == "curl/8.5.0"
    assert captured["timeout"] == 2.5

    captured.clear()
    geoip.default_fetch("https://example.test", None)
    assert captured["handlers"] == ()


def test_lookup_exit_ip_uses_first_provider_that_parses_successfully(monkeypatch):
    calls = []

    def fetch(url, proxy, timeout):
        calls.append((url, proxy, timeout))
        if "ipinfo" in url:
            return "{}"
        return json.dumps({
            "status": "success",
            "countryCode": "GB",
            "query": "203.0.113.8",
            "timezone": "Europe/London",
        })

    result = geoip.lookup_exit_ip("http://proxy:123", fetch=fetch, timeout=3, mmdb="/missing.mmdb")

    assert result == {
        "ip": "203.0.113.8",
        "country": "GB",
        "timezone": "Europe/London",
        "source": "ip-api",
    }
    assert len(calls) == 2
    assert all(call[1:] == ("http://proxy:123", 3) for call in calls)


def test_lookup_exit_ip_prefers_maxmind_when_available(monkeypatch, tmp_path):
    mmdb = tmp_path / "GeoLite.mmdb"
    mmdb.touch()
    calls = []
    monkeypatch.setattr(geoip, "_maxmind_lookup", lambda ip, _path: {
        "ip": ip, "country": "US", "timezone": "America/Chicago",
    })

    def fetch(url, proxy, timeout):
        calls.append((url, proxy, timeout))
        return "203.0.113.9"

    result = geoip.lookup_exit_ip("http://proxy", fetch=fetch, mmdb=str(mmdb))

    assert result == {
        "ip": "203.0.113.9", "country": "US",
        "timezone": "America/Chicago", "source": "maxmind",
    }
    assert len(calls) == 1 and "ipify" in calls[0][0]


def test_maxmind_lookup_returns_city_timezone(monkeypatch):
    class Reader:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def city(self, _ip):
            return types.SimpleNamespace(
                country=types.SimpleNamespace(iso_code="US"),
                location=types.SimpleNamespace(time_zone="America/Chicago"),
            )

    geoip2 = types.ModuleType("geoip2")
    database = types.ModuleType("geoip2.database")
    database.Reader = Reader
    geoip2.database = database
    monkeypatch.setitem(sys.modules, "geoip2", geoip2)
    monkeypatch.setitem(sys.modules, "geoip2.database", database)

    assert geoip._maxmind_lookup("203.0.113.1", "/db") == {
        "ip": "203.0.113.1", "country": "US", "timezone": "America/Chicago",
    }


def test_maxmind_lookup_falls_back_to_country_and_handles_missing_database(monkeypatch):
    class Reader:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def city(self, _ip):
            raise RuntimeError("city record missing")

        def country(self, _ip):
            return types.SimpleNamespace(country=types.SimpleNamespace(iso_code="DE"))

    geoip2 = types.ModuleType("geoip2")
    database = types.ModuleType("geoip2.database")
    database.Reader = Reader
    geoip2.database = database
    monkeypatch.setitem(sys.modules, "geoip2", geoip2)
    monkeypatch.setitem(sys.modules, "geoip2.database", database)
    assert geoip._maxmind_lookup("203.0.113.2", "/db") == {
        "ip": "203.0.113.2", "country": "DE", "timezone": None,
    }

    monkeypatch.delitem(sys.modules, "geoip2.database")
    monkeypatch.delitem(sys.modules, "geoip2")
    assert geoip._maxmind_lookup("203.0.113.2", "/db") is None


def test_maxmind_lookup_returns_none_when_database_fails(monkeypatch, caplog):
    class Reader:
        def __init__(self, _path):
            raise OSError("database unreadable")

    geoip2 = types.ModuleType("geoip2")
    database = types.ModuleType("geoip2.database")
    database.Reader = Reader
    geoip2.database = database
    monkeypatch.setitem(sys.modules, "geoip2", geoip2)
    monkeypatch.setitem(sys.modules, "geoip2.database", database)

    assert geoip._maxmind_lookup("203.0.113.3", "/db") is None
    assert "MaxMind /db" in caplog.text


def test_lookup_exit_ip_accumulates_provider_failures():
    def fail(url, _proxy, _timeout):
        raise OSError(f"failed {url}")

    with pytest.raises(RuntimeError, match="ipinfo.*ip-api"):
        geoip.lookup_exit_ip(None, fetch=fail, mmdb="/missing.mmdb")


def test_resolve_geo_adds_timestamp_and_source(monkeypatch):
    monkeypatch.setattr(geoip, "lookup_exit_ip", lambda *_args, **_kwargs: {
        "ip": "203.0.113.10", "country": "JP", "timezone": "Asia/Tokyo",
        "source": "test",
    })

    result = geoip.resolve_geo("http://proxy", now=1234)

    assert result["country"] == "JP"
    assert result["locale"] == "ja-JP"
    assert result["timezone_id"] == "Asia/Tokyo"
    assert result["geo_source"] == "test"
    assert result["exit_ip"] == "203.0.113.10"
    assert result["checked_at"] == 1234


@pytest.mark.parametrize("profile,ip,now,expected", [
    ({}, None, 100, "no_geo"),
    ({"exit_ip": "1.1.1.1", "checked_at": 50}, "2.2.2.2", 51, "exit_ip_changed"),
    ({"exit_ip": "1.1.1.1", "checked_at": 1}, None, 100, "stale"),
    ({"exit_ip": "1.1.1.1", "checked_at": 90}, "1.1.1.1", 100, None),
])
def test_needs_recheck(profile, ip, now, expected):
    assert geoip.needs_recheck(profile, current_exit_ip=ip, now=now, ttl=20) == expected
