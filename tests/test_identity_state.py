import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import identity  # noqa: E402


@pytest.fixture
def identity_dirs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(identity, "STATE_DIR", state)
    monkeypatch.setattr(identity, "SLOTS_DIR", state / "slots")
    monkeypatch.setattr(identity, "REQUESTS_DIR", state / "requests")
    return state


def test_read_json_handles_missing_invalid_and_unreadable_files(tmp_path, caplog):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{invalid", encoding="utf-8")

    assert identity.read_json(missing) is None
    assert identity.read_json(invalid) is None
    assert "не прочитал" in caplog.text


def test_write_request_is_readable_and_clear_is_idempotent(identity_dirs):
    identity.write_request("account_1", "empty_listing", 6)
    request = identity.read_request("account_1")

    assert request["status"] == "pending"
    assert request["reason"] == "empty_listing"
    assert request["empty_streak"] == 6
    assert isinstance(request["ts"], (float, int))

    identity.clear_request("account_1")
    identity.clear_request("account_1")
    assert identity.read_request("account_1") is None
    assert list((identity_dirs / "requests").iterdir()) == []


def test_slot_profile_reads_only_profiles_with_impersonate(identity_dirs):
    profile_path = identity.slot_profile_path("account_1")
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(json.dumps({"node": "node-a"}), encoding="utf-8")
    assert identity.read_slot_profile("account_1") is None

    profile_path.write_text(
        json.dumps({"impersonate": "chrome131", "node": "node-a"}),
        encoding="utf-8",
    )
    assert identity.read_slot_profile("account_1") == {
        "impersonate": "chrome131",
        "node": "node-a",
    }


def test_write_json_atomic_replaces_destination_without_temp_file(identity_dirs):
    target = identity_dirs / "nested" / "value.json"
    identity.write_json_atomic(target, {"unicode": "привет", "count": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"unicode": "привет", "count": 2}
    assert sorted(path.name for path in target.parent.iterdir()) == ["value.json"]
    assert os.stat(target.parent).st_mode & 0o777 == 0o777
    assert os.stat(target).st_mode & 0o666 == 0o666


def test_slot_profile_watcher_detects_changes_and_ignores_invalid_profile(identity_dirs):
    profile_path = identity.slot_profile_path("account_1")
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text('{"impersonate":"chrome131","node":"one"}', encoding="utf-8")
    watcher = identity.SlotProfileWatcher("account_1", min_check_interval=0)

    assert watcher.load_if_changed(force=True)["node"] == "one"
    assert watcher.load_if_changed(force=True) is None

    profile_path.write_text('{"node":"missing-impersonate"}', encoding="utf-8")
    os.utime(profile_path, ns=(profile_path.stat().st_atime_ns, profile_path.stat().st_mtime_ns + 1_000_000))
    assert watcher.load_if_changed(force=True) is None

    profile_path.write_text('{"impersonate":"firefox147","node":"two"}', encoding="utf-8")
    os.utime(profile_path, ns=(profile_path.stat().st_atime_ns, profile_path.stat().st_mtime_ns + 1_000_000))
    assert watcher.load_if_changed(force=True)["node"] == "two"


def test_slot_profile_watcher_obeys_check_interval_and_missing_path(identity_dirs, monkeypatch):
    watcher = identity.SlotProfileWatcher("missing", min_check_interval=10)
    times = iter((100.0, 105.0, 111.0))
    monkeypatch.setattr(identity.time, "monotonic", lambda: next(times))

    assert watcher.load_if_changed() is None
    assert watcher.load_if_changed() is None
    assert watcher.load_if_changed() is None


def test_clear_request_logs_non_missing_unlink_error(identity_dirs, monkeypatch, caplog):
    request_path = identity.request_path("account_1")
    request_path.parent.mkdir(parents=True)
    request_path.write_text("{}", encoding="utf-8")

    def denied(_path):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", denied)
    identity.clear_request("account_1")

    assert "не удалось удалить заявку" in caplog.text
