import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


class Config:
    def __init__(self, *_args):
        self.loaded = False

    def load_once(self):
        self.loaded = True

    def get(self, _key, default=None):
        return default


def test_main_returns_when_no_accounts_enabled(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(main, "ConfigStore", Config)
    monkeypatch.setattr(main, "load_accounts", lambda: [
        {"name": "waiting", "login": True, "enabled": False},
    ])

    asyncio.run(main.main())

    assert "Нужен вход" in caplog.text
    assert "Нет ни одного enabled:true аккаунта" in caplog.text


def test_install_signal_handlers_registers_supported_signals():
    class Loop:
        def __init__(self):
            self.handlers = []

        def add_signal_handler(self, sig, handler, *args):
            self.handlers.append((sig, handler, args))

    loop = Loop()
    stop = asyncio.Event()

    main._install_signal_handlers(loop, stop)

    assert [entry[0] for entry in loop.handlers] == [signal.SIGTERM, signal.SIGINT]
    for sig, handler, args in loop.handlers:
        handler(*args)
        assert stop.is_set()
        assert args == (sig.name,)


def test_install_signal_handlers_ignores_platforms_without_support(caplog):
    class Loop:
        def add_signal_handler(self, *_args):
            raise NotImplementedError

    caplog.set_level(logging.DEBUG)
    main._install_signal_handlers(Loop(), asyncio.Event())

    assert caplog.text.count("add_signal_handler недоступен") == 2


def test_signal_handler_only_logs_and_sets_stop_event_once(caplog):
    class Loop:
        def add_signal_handler(self, _sig, handler, *args):
            self.handler = handler
            self.args = args

    caplog.set_level(logging.INFO)
    loop = Loop()
    stop = asyncio.Event()
    main._install_signal_handlers(loop, stop)

    loop.handler(*loop.args)
    loop.handler(*loop.args)

    assert stop.is_set()
    assert caplog.text.count("начинаю штатную остановку") == 1
