import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import config as config_module  # noqa: E402
from scraper.config import ConfigStore, CookieFileWatcher, load_accounts, load_cookies  # noqa: E402


class StopReload(Exception):
    pass


def test_config_store_loads_yaml_and_uses_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("stream_subs: [all, news]\nmax_age_seconds: 15\n", encoding="utf-8")
    store = ConfigStore(path, reload_interval=10)

    store.load_once()

    assert store.get("stream_subs") == ["all", "news"]
    assert store.get("max_age_seconds") == 15
    assert store.get("missing", "fallback") == "fallback"


def test_config_store_treats_empty_yaml_as_empty_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    store = ConfigStore(path, reload_interval=10)

    store.load_once()

    assert store.get("anything") is None


def test_load_accounts_and_cookie_files(tmp_path, monkeypatch):
    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text("accounts:\n  - name: first\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "ACCOUNTS_PATH", accounts_path)
    assert load_accounts() == [{"name": "first", "enabled": True}]

    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([
        {"name": "session", "value": "one"},
        {"name": "other", "value": "two"},
        {"name": "invalid"},
    ]), encoding="utf-8")
    assert load_cookies(cookie_path) == {"session": "one", "other": "two"}


def test_cookie_watcher_loads_changed_file_and_respects_check_interval(tmp_path, monkeypatch):
    path = tmp_path / "cookies.json"
    path.write_text('[{"name":"session","value":"first"}]', encoding="utf-8")
    watcher = CookieFileWatcher(path, min_check_interval=5)
    now = 100.0
    monkeypatch.setattr(config_module.time, "monotonic", lambda: now)

    assert watcher.load_if_changed() == {"session": "first"}
    assert watcher.load_if_changed() is None

    path.write_text('[{"name":"session","value":"second"}]', encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 2))
    assert watcher.load_if_changed() is None

    now += 6
    assert watcher.load_if_changed() == {"session": "second"}
    assert watcher.load_if_changed(force=True) is None


@pytest.mark.parametrize("content", ["{invalid", "[]"])
def test_cookie_watcher_ignores_invalid_or_empty_cookie_files(tmp_path, content, caplog):
    path = tmp_path / "cookies.json"
    path.write_text(content, encoding="utf-8")
    watcher = CookieFileWatcher(path)

    assert watcher.load_if_changed(force=True) is None
    assert "оставляю старые" in caplog.text or "игнорирую" in caplog.text


def test_cookie_watcher_ignores_missing_file_and_logs_if_previously_loaded(tmp_path, caplog):
    path = tmp_path / "cookies.json"
    path.write_text('[{"name":"session","value":"first"}]', encoding="utf-8")
    watcher = CookieFileWatcher(path)
    assert watcher.load_if_changed(force=True) == {"session": "first"}

    path.unlink()
    assert watcher.load_if_changed(force=True) is None
    assert "Не удалось получить mtime" in caplog.text


def test_config_reload_loop_logs_updates_and_keeps_old_config_on_error(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    path = tmp_path / "config.yaml"
    path.write_text("stream_subs: [all]\nmax_age_seconds: 10\n", encoding="utf-8")
    store = ConfigStore(path, reload_interval=1)
    store.load_once()
    path.write_text(
        "stream_subs: [news]\nmax_age_seconds: 20\nqueue_control:\n  enabled: true\n"
        "target_rate_per_second: 5\nbatch_max_items: 10\n",
        encoding="utf-8",
    )

    sleeps = 0

    async def sleep(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise StopReload

    monkeypatch.setattr(config_module.asyncio, "sleep", sleep)
    with pytest.raises(StopReload):
        asyncio.run(store.reload_loop())

    assert store.get("max_age_seconds") == 20
    assert "config.yaml обновлён" in caplog.text

    store.load_once = lambda: (_ for _ in ()).throw(OSError("read failed"))
    sleeps = 0

    async def sleep_again(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise StopReload

    monkeypatch.setattr(config_module.asyncio, "sleep", sleep_again)
    with pytest.raises(StopReload):
        asyncio.run(store.reload_loop())
    assert store.get("max_age_seconds") == 20
    assert "оставляю старые значения" in caplog.text
