import asyncio
import logging
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


class Config:
    def __init__(self, *_args):
        self.values = {
            "metrics": {"enabled": False},
            "queue_control": {"enabled": False},
            "stream_subs": ["all"],
            "target_rate_per_second": 80,
            "max_age_seconds": 15,
            "stream_start_interval_seconds": 1.5,
        }

    def load_once(self):
        return None

    def get(self, key, default=None):
        return self.values.get(key, default)

    async def reload_loop(self):
        await asyncio.Event().wait()


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Executor:
    def __init__(self, calls):
        self.calls = calls

    def shutdown(self, **kwargs):
        self.calls.append(kwargs)


class Sender:
    def __init__(self, *_args, **_kwargs):
        pass

    def demand(self):
        return 1

    async def run(self, fail=False):
        if fail:
            raise RuntimeError("sender failed")
        await asyncio.Event().wait()


class Scheduler:
    def __init__(self, *_args, **_kwargs):
        pass

    async def run(self):
        await asyncio.Event().wait()

    async def stats_loop(self, *_args, **_kwargs):
        await asyncio.Event().wait()


class Metrics:
    def __init__(self, **_kwargs):
        self.attached = False

    def attach(self, **_kwargs):
        self.attached = True


class Journal:
    def __init__(self, *_args, **_kwargs):
        pass


async def wait_forever(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.fixture
def main_stubs(monkeypatch):
    shutdowns = []
    cleanup = []
    metrics = Metrics()
    sender = Sender()

    async def sender_run():
        if getattr(main_stubs, "fail_sender", False):
            raise RuntimeError("sender failed")
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("sender")

    sender.run = sender_run

    def signal_handler(_loop, _event):
        return None

    monkeypatch.setattr(main, "ConfigStore", Config)
    monkeypatch.setattr(main, "load_accounts", lambda: [{"name": "acct", "enabled": True}])
    monkeypatch.setattr(main, "ExecutorHandle", lambda **_kwargs: Executor(shutdowns))
    monkeypatch.setattr(main, "ExecutorHealth", lambda: object())
    monkeypatch.setattr(main, "Sender", lambda *_args, **_kwargs: sender)
    monkeypatch.setattr(main, "PollScheduler", Scheduler)
    monkeypatch.setattr(main, "health_report_loop", wait_forever)
    monkeypatch.setattr(main, "account_worker", wait_forever)
    monkeypatch.setattr(main, "supervised", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(main, "_install_signal_handlers", signal_handler)
    monkeypatch.setattr(main.aiohttp, "TCPConnector", lambda **_kwargs: object())
    monkeypatch.setattr(main.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(main, "MetricsCollector", lambda **_kwargs: metrics)
    monkeypatch.setattr(main, "EventJournal", Journal)

    async def serve(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("metrics")

    monkeypatch.setattr(main, "serve_metrics", serve)
    return shutdowns, cleanup, metrics, sender


def test_main_stops_when_all_workers_finish(main_stubs, monkeypatch, caplog):
    shutdowns, cleanup, _, _ = main_stubs
    caplog.set_level(logging.ERROR)

    async def completed_worker(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "account_worker", completed_worker)

    asyncio.run(main.main())

    assert "Все воркеры аккаунтов завершились" in caplog.text
    assert shutdowns == [{"wait": False, "cancel_futures": True}]


def test_main_reports_worker_exception_after_all_workers_finish(
    main_stubs, monkeypatch, caplog
):
    shutdowns, _, _, _ = main_stubs
    caplog.set_level(logging.ERROR)

    async def failed_worker(*_args, **_kwargs):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(main, "account_worker", failed_worker)
    asyncio.run(main.main())

    assert "Воркер account_worker[acct] завершился с ошибкой" in caplog.text
    assert "Все воркеры аккаунтов завершились" in caplog.text
    assert shutdowns == [{"wait": False, "cancel_futures": True}]


def test_main_stops_on_failed_core_task_and_cancels_everything(
    main_stubs, monkeypatch, caplog
):
    shutdowns, cleanup, _, sender = main_stubs
    caplog.set_level(logging.ERROR)
    sender.run = lambda: _fail_sender(cleanup)

    asyncio.run(main.main())

    assert "Ядро: задача sender завершилась с ошибкой" in caplog.text
    assert shutdowns == [{"wait": False, "cancel_futures": True}]
    assert "metrics" not in cleanup


def test_main_skips_cancelled_core_task_when_reporting_first_completion(
    main_stubs, monkeypatch, caplog
):
    shutdowns, _, _, _ = main_stubs
    caplog.set_level(logging.ERROR)
    create_task = asyncio.create_task

    def create_cancelled_reload_task(coro, *, name=None, context=None):
        task = create_task(coro, name=name, context=context)
        if name == "config_reload_loop":
            task.cancel()
        return task

    monkeypatch.setattr(main.asyncio, "create_task", create_cancelled_reload_task)
    asyncio.run(main.main())

    assert "Ядро: задача" not in caplog.text
    assert shutdowns == [{"wait": False, "cancel_futures": True}]


def test_main_starts_metrics_as_auxiliary_task(main_stubs, monkeypatch):
    shutdowns, cleanup, metrics, sender = main_stubs
    config = Config()
    config.values["metrics"] = {"enabled": True, "events_max_bytes": 1000, "events_backups": 2}
    monkeypatch.setattr(main, "ConfigStore", lambda *_args: config)
    sender.run = lambda: _fail_sender(cleanup)

    asyncio.run(main.main())

    assert metrics.attached
    assert shutdowns == [{"wait": False, "cancel_futures": True}]
    assert "metrics" in cleanup


def test_main_logs_keyboard_interrupt_from_module_entrypoint(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def interrupt_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupt_run)
    runpy.run_path(str(ROOT / "main.py"), run_name="__main__")

    assert "Остановлено пользователем (KeyboardInterrupt)" in caplog.text


async def _fail_sender(cleanup):
    cleanup.append("sender-failed")
    raise RuntimeError("sender failed")
