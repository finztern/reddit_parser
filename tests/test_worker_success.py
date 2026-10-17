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
from scraper.scheduler import PollOutcome  # noqa: E402
from scraper.state import SeenCache  # noqa: E402


class Config:
    def __init__(self):
        self.values = {
            "cookie_reload_check_interval_seconds": 0,
            "max_age_seconds": 60,
            "pagination_max_pages": 2,
            "pagination_delay_min_seconds": 0,
            "pagination_delay_max_seconds": 0,
            "respect_ratelimit_headers": False,
            "request_timeout_seconds": 10,
            "fetch_limit": 150,
            "stream_subs": ["python", "learnpython"],
            "rotation": {"enabled": False},
            "fleet": {},
            "fleet_polling": {},
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class CurlSession:
    def __init__(self, **kwargs):
        self.accept_language = "en-US"
        self.proxies = kwargs["proxies"]
        self.impersonate = kwargs["impersonate"]
        self.swaps = 0
        self.closed = False

    def set_profile_headers(self, _profile):
        return None

    def set_locale(self, locale):
        self.accept_language = locale

    def close(self):
        self.closed = True


class SlotQueue:
    def __init__(self):
        self.calls = 0

    async def get(self):
        self.calls += 1
        if self.calls > 1:
            raise asyncio.CancelledError
        return None


class Scheduler:
    def __init__(self):
        self.slot = type("Slot", (), {
            "queue": SlotQueue(),
            "health": AccountHealth("acct", now=1),
        })()
        self.releases = []
        self.removed = []

    def register(self, name):
        assert name == "acct"
        return self.slot

    def cfg(self):
        return FleetPollingConfig.from_getter(Config().get)

    def now(self):
        return 1.0

    def release(self, slot, outcome, cooldown, sent, kind):
        self.releases.append((slot, outcome, cooldown, sent, kind))

    def unregister(self, slot):
        self.removed.append(slot)


class Sender:
    backlog = 0

    def __init__(self):
        self.items = []

    async def put_many(self, items):
        self.items.extend(items)
        self.backlog = len(self.items)
        return len(items)


def test_account_worker_claims_new_comments_and_reports_poll_outcome(tmp_path, monkeypatch):
    cookie = tmp_path / "cookies.json"
    cookie.write_text(json.dumps([{"name": "session", "value": "cookie"}]), encoding="utf-8")
    monkeypatch.setattr(worker, "BASE_DIR", tmp_path)
    monkeypatch.setattr(worker.aiohttp, "ClientSession", ClientSession)
    monkeypatch.setattr(worker, "CurlSessionHandle", CurlSession)
    monkeypatch.setattr(worker, "load_device_profile", lambda _name: None)
    monkeypatch.setattr(worker.identity, "read_request", lambda _name: None)

    class ProfileWatcher:
        def __init__(self, _name):
            pass

        def load_if_changed(self, force=False):
            return None

    monkeypatch.setattr(worker.identity, "SlotProfileWatcher", ProfileWatcher)
    now = time.time()
    seen = SeenCache(10)
    scheduler = Scheduler()
    sender = Sender()
    assert asyncio.run(seen.try_claim("t1_already_seen"))
    fetch_calls = []

    async def fetch_comments(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        page = [
            {"name": "t1_already_seen", "id": "already_seen", "created_utc": now},
            {
                "name": "t1_new",
                "id": "new",
                "created_utc": now,
                "body": "new comment",
                "author": "user",
                "subreddit": "python",
            },
            {"id": "too_old", "created_utc": now - 120},
            {"created_utc": now},
        ]
        assert kwargs["stop_when"](page)
        return FetchResult(comments=page, status=200, pages_fetched=2)

    monkeypatch.setattr(worker, "fetch_comments", fetch_comments)
    config = Config()
    account = {
        "name": "acct",
        "cookie_file": str(cookie),
        "proxy_port": 1234,
        "user_agent": "test-UA",
        "impersonate": "chrome131",
    }

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.account_worker(
            account, config, sender, seen, scheduler, object(),
        ))

    assert len(fetch_calls) == 1
    args, kwargs = fetch_calls[0]
    assert args[1:4] == ("https://www.reddit.com", "python+learnpython", 100)
    assert kwargs["stop_when"] is not None
    assert [entry["external_id"] for entry in sender.items] == ["t1_new"]
    assert seen.contains("t1_new") and "t1_new" in seen._pending
    assert seen.contains("t1_already_seen")

    assert len(scheduler.releases) == 1
    _, outcome, cooldown, sent, kind = scheduler.releases[0]
    assert outcome == PollOutcome(page_len=4, dupes=1, pages=2, overlap_found=True)
    assert cooldown == 0
    assert sent == 1
    assert kind == "ok"
    assert scheduler.removed == [scheduler.slot]
