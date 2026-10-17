import builtins
import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import geoip, profile_gen as pg  # noqa: E402


def profile(**kwargs):
    geo = geoip.geo_for_country("US")
    geo.update(geo_source="test", exit_ip="203.0.113.20", checked_at=1_700_000_000)
    return pg.generate_profile("acct", 5, geo=geo, created_at=1_700_000_000, **kwargs)


def test_load_presets_caches_valid_content_and_reports_missing_sections(tmp_path):
    valid_path = tmp_path / "valid.yaml"
    valid_path.write_text(yaml.safe_dump({
        "platform_weights": {}, "browser_weights": {}, "targets": {},
        "platforms": {}, "webgl": {}, "tiers": {},
    }), encoding="utf-8")

    loaded = pg.load_presets(valid_path)
    valid_path.write_text("broken: true\n", encoding="utf-8")
    assert pg.load_presets(valid_path) is loaded

    missing_path = tmp_path / "missing.yaml"
    missing_path.write_text("platform_weights: {}\n", encoding="utf-8")
    with pytest.raises(pg.ProfileError, match="нет секции"):
        pg.load_presets(missing_path)


def test_available_targets_falls_back_when_curl_cffi_cannot_be_imported(monkeypatch):
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "curl_cffi.requests":
            raise ImportError("curl_cffi unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert pg.available_targets({}) is None


def test_parse_target_rejects_empty_and_malformed_values():
    for target in (None, "", "chrome", "firefox", "safari_ios"):
        with pytest.raises(ValueError, match="неподдерживаемый"):
            pg.parse_target(target)


def test_safari_version_uses_preset_then_derives_fallback():
    preset = {"family": "safari", "digits": "260", "mobile": None, "id": "safari260"}
    assert pg._safari_version(preset, {"safari_versions": {"safari260": "26.0"}}) == "26.0"
    assert pg._safari_version(preset, {}) == "26.0"

    ios = {"family": "safari", "digits": "184", "mobile": "ios", "id": "safari184_ios"}
    assert pg._safari_version(ios, {"ios_versions": {"safari184_ios": ["18_4", "18.4"]}}) == "18.4"
    assert pg._ios_os_string(ios, {}) == "18_4"


def test_weight_choice_uses_last_row_if_rng_exceeds_total():
    class Rng:
        def random(self):
            return 2

    assert pg._wchoice(Rng(), [{"id": "first", "wt": 1}, {"id": "last", "wt": 1}]) == {
        "id": "last", "wt": 1,
    }


def test_generate_profile_reports_missing_webgl_template():
    presets = copy.deepcopy(pg.load_presets())
    presets["webgl"]["windows"].pop("chrome")

    with pytest.raises(pg.ProfileError, match="нет WebGL-шаблона"):
        pg.generate_profile("acct", 1, os_name="windows", impersonate="chrome131", presets=presets)


def test_profile_from_account_preserves_known_device_and_rejects_bad_input():
    geo = geoip.geo_for_country("DE")
    geo.update(geo_source="test", exit_ip="203.0.113.21", checked_at=1_700_000_000)
    ua = profile(os_name="windows", impersonate="chrome131")["user_agent"]
    migrated = pg.profile_from_account(
        {"name": "legacy", "impersonate": "chrome131", "user_agent": ua},
        geo,
        created_at=1_700_000_000,
    )
    assert migrated["account"] == "legacy"
    assert migrated["source"] == "accounts.yaml"
    assert migrated["user_agent"] == ua
    assert migrated["country"] == "DE"

    with pytest.raises(pg.ProfileError, match="нужны impersonate и user_agent"):
        pg.profile_from_account({"name": "missing"}, geo)
    with pytest.raises(pg.ProfileError, match="не могу определить платформу"):
        pg.profile_from_account({
            "name": "unknown", "impersonate": "chrome131", "user_agent": "unknown-UA",
        }, geo)


@pytest.mark.parametrize("mutator,expected", [
    (lambda item: item.update(impersonate="unsupported"), "неподдерживаемый impersonate"),
    (lambda item: item.update(os="unknown"), "неизвестная платформа"),
])
def test_validate_profile_returns_structural_errors(mutator, expected):
    candidate = profile(os_name="windows", impersonate="chrome131")
    mutator(candidate)
    assert any(expected in error for error in pg.validate_profile(candidate))


def test_validate_profile_reports_invalid_screen_and_geo_fields(monkeypatch):
    candidate = profile(os_name="windows", impersonate="chrome131")
    candidate["screen"] = {"w": "wide", "h": 1080}
    candidate["locale"] = "en-GB"
    candidate["country"] = "ZZ"
    monkeypatch.setattr(pg, "build_accept_language", lambda *_args: (_ for _ in ()).throw(ValueError("bad locale")))

    errors = pg.validate_profile(candidate)
    assert "screen/viewport должны быть {w,h}" in errors
    geo_errors = pg.validate_geo(candidate)
    assert any("country='ZZ'" in error for error in geo_errors)


def test_generate_fleet_raises_after_all_uniqueness_attempts(monkeypatch):
    monkeypatch.setattr(pg, "generate_profile", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pg, "fingerprint", lambda _profile: "same")

    with pytest.raises(pg.ProfileError, match="за 200 попыток"):
        pg.generate_fleet(["acct"], base_seed=1, presets={"custom": True}, taken={"same"})
