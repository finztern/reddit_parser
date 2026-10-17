import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import worker  # noqa: E402
from scraper.fleet_polling import AccountHealth, FleetPollingConfig  # noqa: E402
from scraper.models import FetchResult  # noqa: E402
from scraper.state import SeenCache  # noqa: E402


class Config:
    def __init__(self, **overrides):
        self.values = {
            "cookie_reload_check_interval_seconds": 0,
            "max_age_seconds": 60,
            "pagination_max_pages": 2,
            "pagination_delay_min_seconds": 0,
            "pagination_delay_max_seconds": 0,
            "respect_ratelimit_headers": False,
            "request_timeout_seconds": 10,
            "fetch_limit": 100,
            "stream_subs": ["all"],
            "rotation": {"enabled": False},
            "fleet": {},
            "fleet_polling": {},
            **overrides,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class CookieJar:
    def __init__(self):
        self.cookies = {}

    def clear(self):
        self.cookies.clear()

    def update_cookies(self, cookies):
        self.cookies.update(cookies)


class ClientSession:
    def __init__(self, **kwargs):
        self.cookies = kwargs["cookies"]
        self.headers = kwargs["headers"]
        self.cookie_jar = CookieJar()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True


class CurlSession:
    instances = []

    def __init__(self, **kwargs):
        self.accept_language = "en-US"
        self.proxies = kwargs["proxies"]
        self.impersonate = kwargs["impersonate"]
        self.swaps = 0
        self.closed = False
        self.locales = []
        self.impersonates = []
        self.instances.append(self)

    def set_profile_headers(self, _profile):
        return None

    def set_locale(self, locale):
        self.locales.append(locale)
        self.accept_language = locale

    def set_impersonate(self, impersonate):
        self.impersonates.append(impersonate)
        self.impersonate = impersonate

    def swap(self):
        self.swaps += 1

    def close(self):
        self.closed = True


class Queue:
    def __init__(self, poll_limit=1):
        self.calls = 0
        self.poll_limit = poll_limit

    async def get(self):
        self.calls += 1
        if self.calls > self.poll_limit:
            raise asyncio.CancelledError


class Scheduler:
    def __init__(self, poll_limit=1, **fleet):
        self.fp = FleetPollingConfig.from_getter(
            Config(fleet_polling=fleet).get
        )
        self.slot = type("Slot", (), {
            "queue": Queue(poll_limit),
            "health": AccountHealth("acct", now=1),
        })()
        self.releases = []
        self.unregistered = []

    def cfg(self):
        return self.fp

    def now(self):
        return 1.0

    def register(self, name):
        assert name == "acct"
        return self.slot

    def release(self, slot, outcome, cooldown, sent, kind):
        self.releases.append((slot, outcome, cooldown, sent, kind))

    def unregister(self, slot):
        self.unregistered.append(slot)


class Sender:
    backlog = 0

    def __init__(self):
        self.items = []

    async def put_many(self, items):
        self.items.extend(items)
        return len(items)


class Metrics:
    def __init__(self):
        self.events = []
        self.polls = []

    def event(self, *args):
        self.events.append(args)

    def record_poll(self, *args, **kwargs):
        self.polls.append((args, kwargs))


def make_watcher(class_name, values):
    class Watcher:
        def __init__(self, *_args, **_kwargs):
            self.values = list(values)
            self.calls = []
            self.min_check_interval = None

        def load_if_changed(self, force=False):
            self.calls.append(force)
            return self.values.pop(0) if self.values else None

    Watcher.__name__ = class_name
    return Watcher


def prepare_worker(tmp_path, monkeypatch, *, result, config=None, account=None,
                   request=None, slot_profiles=(), cookies=({"session": "initial"},), device_profile=None,
                   metrics=None, fleet=None, curl_session_cls=CurlSession, poll_limit=1,
                   fetch_impl=None):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps([{"name": "session", "value": "initial"}]), encoding="utf-8")
    monkeypatch.setattr(worker, "BASE_DIR", tmp_path)
    monkeypatch.setattr(worker.aiohttp, "ClientSession", ClientSession)
    CurlSession.instances.clear()
    monkeypatch.setattr(worker, "CurlSessionHandle", curl_session_cls)
    monkeypatch.setattr(worker, "load_device_profile", lambda _name: device_profile)
    monkeypatch.setattr(worker.identity, "read_request", lambda _name: request)
    monkeypatch.setattr(worker.identity, "clear_request", lambda _name: cleared.append(_name))
    cleared = []
    monkeypatch.setattr(
        worker.identity, "SlotProfileWatcher",
        make_watcher("SlotProfileWatcher", slot_profiles),
    )
    cookie_watcher = make_watcher("CookieFileWatcher", cookies)
    monkeypatch.setattr(worker, "CookieFileWatcher", cookie_watcher)

    async def fetch(*_args, **_kwargs):
        return result

    monkeypatch.setattr(worker, "fetch_comments", fetch_impl or fetch)

    sched = Scheduler(poll_limit=poll_limit, **(fleet or {}))
    sender = Sender()
    conf = config or Config()
    acct = {
        "name": "acct",
        "cookie_file": "cookies.json",
        "proxy_port": 1234,
        "user_agent": "account-UA",
        "impersonate": "chrome131",
        **(account or {}),
    }
    return conf, acct, sender, sched, cookie_file, metrics, cleared


def run_one_poll(monkeypatch, tmp_path, **kwargs):
    releases, *details = run_polls(monkeypatch, tmp_path, poll_limit=1, **kwargs)
    return releases[0], *details


def run_polls(monkeypatch, tmp_path, *, poll_limit, **kwargs):
    initial_health_state = kwargs.pop("initial_health_state", None)
    conf, account, sender, scheduler, cookie_file, metrics, cleared = prepare_worker(
        tmp_path, monkeypatch, poll_limit=poll_limit, **kwargs
    )
    if initial_health_state:
        scheduler.slot.health.state = initial_health_state
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.account_worker(
            account, conf, sender, SeenCache(20), scheduler,
            object(), metrics=metrics,
        ))
    assert scheduler.unregistered == [scheduler.slot]
    assert len(scheduler.releases) == poll_limit
    return scheduler.releases, scheduler, sender, cookie_file, metrics, cleared


@pytest.mark.parametrize(
    "result,kind,state",
    [
        (FetchResult(status=429, error_kind="rate_or_server", retry_after=12), "rate_or_server", "cooldown"),
        (FetchResult(status=401, error_kind="auth"), "auth", "active"),
        (FetchResult(status=200, error_kind="empty"), "empty", "active"),
        (FetchResult(error_kind="network"), "network", "active"),
    ],
)
def test_worker_failure_outcomes_are_reported_and_counted(
    tmp_path, monkeypatch, result, kind, state
):
    metrics = Metrics()
    (slot, outcome, cooldown, sent, actual_kind), scheduler, _, _, metrics, _ = run_one_poll(
        monkeypatch, tmp_path, result=result, metrics=metrics
    )

    assert slot is scheduler.slot
    assert outcome is None
    assert cooldown >= 0
    assert sent == 0
    assert actual_kind == kind
    assert scheduler.slot.health.state == state
    assert len(metrics.polls) == 1
    assert metrics.polls[0][0][1] == kind


def test_worker_waits_on_pending_rotation_without_polling_reddit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "fetch_comments", lambda *_args, **_kwargs: calls.append(True))
    request = {"status": "pending", "ts": time.time(), "reason": "rotation"}

    release, scheduler, _, _, metrics, cleared = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(comments=[]),
        config=Config(rotation={"enabled": True, "slots": ["acct"]}),
        request=request,
        metrics=Metrics(),
    )

    assert calls == []
    assert release[1] is None
    assert release[2] == worker.ROTATION_POLL_SECONDS
    assert release[4] == "skipped"
    assert cleared == []
    assert metrics.polls == []
    assert scheduler.slot.health.state == "active"


def test_worker_clears_expired_rotation_request_then_polls(tmp_path, monkeypatch):
    request = {"status": "working", "ts": time.time() - 100, "reason": "stuck"}

    release, _, _, _, _, cleared = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        config=Config(rotation={
            "enabled": True, "slots": ["acct"], "request_timeout_seconds": 10,
        }),
        request=request,
    )

    assert cleared == ["acct"]
    assert release[4] == "ok"


def test_worker_continues_after_rotation_is_rejected(tmp_path, monkeypatch):
    release, _, _, _, _, cleared = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        config=Config(rotation={"enabled": True, "slots": ["acct"]}),
        request={
            "status": "rejected",
            "ts": time.time(),
            "retry_after": time.time() + 600,
            "reason": "capacity",
        },
    )

    assert cleared == ["acct"]
    assert release[4] == "ok"


def test_worker_applies_completed_rotation_and_refreshes_cookies(tmp_path, monkeypatch):
    metrics = Metrics()
    profiles = [
        None,
        {"impersonate": "chrome131", "user_agent": "profile-UA", "node": "node-b"},
    ]

    release, _, _, cookie_file, metrics, cleared = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        config=Config(rotation={"enabled": True, "slots": ["acct"]}),
        request={"status": "done", "ts": time.time(), "retry_after": 0},
        slot_profiles=profiles,
        cookies=[{"session": "initial"}, {"session": "rotated"}],
        metrics=metrics,
    )

    assert cleared == ["acct"]
    assert release[4] == "ok"
    assert any(event[1] == "node_replaced" for event in metrics.events)
    assert CurlSession.instances[-1].swaps == 1
    assert cookie_file.exists()


def test_worker_applies_profile_and_locale_updates_and_refreshes_cookies(tmp_path, monkeypatch):
    profiles = [
        {"impersonate": "chrome131", "node": "node-a"},
        {"impersonate": "firefox147", "node": "node-b", "geo": {"locale": "de-DE"}},
    ]

    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        slot_profiles=profiles,
        cookies=[{"session": "initial"}, {"session": "rotated"}],
        account={"locale": "en-US"},
    )

    curl = CurlSession.instances[-1]
    assert release[4] == "ok"
    assert curl.locales == ["de-DE"]
    assert curl.impersonates == ["firefox147"]
    assert curl.swaps == 1


def test_worker_keeps_working_when_profile_impersonate_is_not_supported(tmp_path, monkeypatch):
    class UnsupportedCurl(CurlSession):
        def set_impersonate(self, _impersonate):
            raise ValueError("unsupported browser target")

    profiles = [
        {"impersonate": "chrome131", "node": "node-a"},
        {"impersonate": "firefox147", "node": "node-a"},
    ]

    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        slot_profiles=profiles,
        curl_session_cls=UnsupportedCurl,
    )

    assert release[4] == "ok"
    assert UnsupportedCurl.instances[-1].impersonates == []


def test_worker_keeps_existing_geo_if_refresh_after_node_change_fails(tmp_path, monkeypatch):
    ident = {
        "impersonate": "chrome131", "user_agent": "device-UA", "os": "windows",
        "locale": "en-US", "timezone_id": "America/New_York",
    }
    profiles = [
        {"impersonate": "chrome131", "node": "node-a"},
        {"impersonate": "chrome131", "node": "node-b"},
    ]

    def fail_refresh(*_args):
        raise OSError("geo provider unavailable")

    monkeypatch.setattr(worker, "refresh_profile_geo", fail_refresh)
    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        device_profile=ident,
        slot_profiles=profiles,
    )

    assert release[4] == "ok"
    assert CurlSession.instances[-1].locales == []


def test_worker_retries_cookie_load_until_file_is_valid(tmp_path, monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_wait)
    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        cookies=[None, {"session": "recovered"}],
    )

    assert release[4] == "ok"


def test_new_cookies_reset_auth_failed_health_and_emit_relogin_event(tmp_path, monkeypatch):
    metrics = Metrics()

    release, scheduler, _, _, metrics, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        cookies=[{"session": "initial"}, {"session": "refreshed"}],
        metrics=metrics,
        initial_health_state="auth_failed",
    )

    assert release[4] == "ok"
    assert scheduler.slot.health.state == "active"
    assert any(event[1] == "relogin_ok" for event in metrics.events)


def test_new_cookies_clear_auth_dead_backoff_before_next_auth_probe(tmp_path, monkeypatch):
    metrics = Metrics()
    releases, scheduler, _, _, metrics, _ = run_polls(
        monkeypatch,
        tmp_path,
        poll_limit=2,
        result=FetchResult(status=401, error_kind="auth"),
        config=Config(
            fleet_polling={"auth_failed_after": 1},
            auth_dead_cooldown_seconds=1,
        ),
        cookies=[{"session": "initial"}, None, {"session": "recovered"}],
        metrics=metrics,
        fleet={"auth_failed_after": 1},
    )

    assert [release[4] for release in releases] == ["auth", "auth"]
    assert scheduler.slot.health.state == "auth_failed"
    assert any(event[1] == "relogin_ok" for event in metrics.events)


def test_worker_pagination_callback_returns_false_without_overlap(tmp_path, monkeypatch):
    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        fetch_impl=_fetch_with_no_overlap,
    )
    assert release[4] == "ok"


async def _fetch_with_no_overlap(*_args, **kwargs):
    assert kwargs["stop_when"]([{"name": "t1_not_seen"}]) is False
    return FetchResult(status=200, comments=[], pages_fetched=1)


def test_worker_requests_rotation_after_empty_streak_threshold(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setattr(
        worker.identity, "write_request",
        lambda *args: requests.append(args),
    )

    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, error_kind="empty"),
        config=Config(rotation={
            "enabled": True, "slots": ["acct"], "empty_streak_trigger": 1,
            "request_cooldown_seconds": 600,
        }),
    )

    assert requests == [("acct", "empty_listing", 1)]
    assert release[2] == worker.ROTATION_POLL_SECONDS
    assert release[4] == "empty"


def test_login_403_requests_node_replacement_after_auth_threshold(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setattr(
        worker.identity, "write_request",
        lambda *args: requests.append(args),
    )

    release, scheduler, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=403, error_kind="auth"),
        config=Config(
            rotation={"enabled": True, "request_cooldown_seconds": 600},
            fleet={"node_replace_only": True},
            auth_dead_cooldown_seconds=120,
        ),
        account={"login": True},
        fleet={"auth_failed_after": 1},
    )

    assert requests == [("acct", "blocked_403", 1)]
    assert release[2] == worker.ROTATION_POLL_SECONDS
    assert release[4] == "auth"
    assert scheduler.slot.health.state == "blocked_node"


def test_worker_continues_if_rotation_request_cannot_be_written(tmp_path, monkeypatch):
    def fail_write(*_args):
        raise OSError("state is read-only")

    monkeypatch.setattr(worker.identity, "write_request", fail_write)
    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, error_kind="empty"),
        config=Config(rotation={
            "enabled": True, "slots": ["acct"], "empty_streak_trigger": 1,
        }),
    )

    assert release[4] == "empty"
    assert release[2] >= 0


def test_login_403_continues_if_node_replacement_request_cannot_be_written(tmp_path, monkeypatch):
    def fail_write(*_args):
        raise OSError("state is read-only")

    monkeypatch.setattr(worker.identity, "write_request", fail_write)
    release, scheduler, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=403, error_kind="auth"),
        config=Config(
            rotation={"enabled": True, "request_cooldown_seconds": 600},
            fleet={"node_replace_only": True},
            auth_dead_cooldown_seconds=1,
        ),
        account={"login": True},
        fleet={"auth_failed_after": 1},
    )

    assert release[4] == "auth"
    assert scheduler.slot.health.state == "auth_failed"


def test_worker_refreshes_geo_when_rotation_changes_node(tmp_path, monkeypatch):
    ident = {
        "impersonate": "chrome131",
        "user_agent": "device-UA",
        "os": "windows",
        "locale": "en-US",
        "timezone_id": "America/New_York",
    }
    profiles = [
        {"impersonate": "chrome131", "node": "node-a"},
        {"impersonate": "chrome131", "node": "node-b"},
    ]
    refreshed = {**ident, "locale": "de-DE"}
    calls = []
    monkeypatch.setattr(worker, "refresh_profile_geo", lambda name, proxy: calls.append((name, proxy)) or refreshed)

    release, _, _, _, _, _ = run_one_poll(
        monkeypatch,
        tmp_path,
        result=FetchResult(status=200, comments=[]),
        device_profile=ident,
        slot_profiles=profiles,
    )

    assert release[4] == "ok"
    assert calls == [("acct", "http://127.0.0.1:1234")]
    assert CurlSession.instances[-1].locales == ["de-DE"]
