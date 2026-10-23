#!/usr/bin/env python3
"""
scripts/registration_hook.py — ТВОЯ логика автоматической регистрации аккаунта.

Хаб (`login_hub.py --register`) и `manual_login.py --register` сами делают всё остальное:
  * поднимают браузер с профилем слота (fingerprint, locale, timezone) через прокси слота;
  * генерируют ник и пароль (scripts/account_creds.py -> credentials/<account>.json, 0600);
  * открывают <base_url>/register и вызывают функции ниже;
  * каждые ~3 с проверяют, залогинен ли аккаунт; как только вход обнаружен — сохраняют cookies
    и storage_state, включают слот, обновляют credentials/<account>.json (status=logged_in).

Здесь нужно реализовать только два места (помечены «ТВОЙ КОД»). Пока они пустые — окно просто
открывается на /register, ник и пароль напечатаны в консоли, всё остальное делаешь руками в noVNC.

Правила:
  * `page` — синхронный Playwright Page. Не закрывай page/context/browser — это делает хаб.
  * Не блокируй надолго: on_tick вызывается в том же потоке, что и ожидание входа; пока он работает,
    вход не проверяется. Долгие шаги дроби на тики (храни прогресс в ctx.state).
  * Исключения не роняют сессию: они логируются (один раз на тип), после 20 подряд упавших
    on_tick хук отключается до конца сессии.
  * Не пиши пароль и куки в логи: ctx.log() идёт в консоль оператора.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_creds as ac  # noqa: E402


@dataclass
class RegCtx:
    account: str            # имя слота, например "account_60"
    username: str           # сгенерированный ник
    password: str           # сгенерированный пароль
    base_url: str           # https://www.reddit.com
    proxy_url: str          # прокси слота (http://host:port), через него ходит и браузер
    impersonate: str        # curl_cffi-профиль слота (если нужны запросы вне браузера)
    log: Callable[[str], None] = print
    state: dict = field(default_factory=dict)   # свободное хранилище между вызовами (шаги, флаги)

    def save_creds(self, **fields) -> None:
        """Дописать поля (email, recovery и т.п.) в credentials/<account>.json."""
        cur = ac.load_credentials(self.account) or {"account": self.account, "username": self.username,
                                                    "password": self.password}
        cur.update(fields)
        ac.save_credentials(self.account, cur)


def on_page_ready(page, ctx: RegCtx) -> None:
    """Вызывается ОДИН раз после page.goto(<base_url>/register).

    ============================ ТВОЙ КОД: начало ============================
    Например: дождаться формы, заполнить ник/пароль/почту, нажать «Продолжить».
        page.wait_for_selector('input[name="email"]', timeout=15000)
        page.fill('input[name="email"]', "...")
        ctx.save_creds(email="...")
    ============================= ТВОЙ КОД: конец ============================
    """


def on_tick(page, ctx: RegCtx) -> None:
    """Вызывается каждые ~3 с, пока вход не обнаружен (после on_page_ready).

    Для многошаговых форм: смотри на page.url / наличие элементов и храни прогресс в ctx.state,
    например `if ctx.state.get("step") == 1: ...; ctx.state["step"] = 2`.

    ============================ ТВОЙ КОД: начало ============================
    ============================= ТВОЙ КОД: конец ============================
    """


# ---------------------------------------------------------------------------
# Дальше — обвязка, её менять не нужно.
# ---------------------------------------------------------------------------

class HookRunner:
    MAX_TICK_FAILS = 20

    def __init__(self, ctx: RegCtx):
        self.ctx, self._ready, self._fails, self._seen = ctx, False, 0, set()

    def _call(self, fn, page) -> bool:
        try:
            fn(page, self.ctx)
            return True
        except Exception as e:  # noqa: BLE001 — хук не должен ронять сессию логина
            key = (fn.__name__, type(e).__name__, str(e)[:80])
            if key not in self._seen:
                self._seen.add(key)
                self.ctx.log(f"[{self.ctx.account}] registration_hook.{fn.__name__}: {type(e).__name__}: {e}")
                self.ctx.log(traceback.format_exc(limit=3).rstrip())
            return False

    def page_ready(self, page) -> None:
        if not self._ready:
            self._ready = True
            self._call(on_page_ready, page)

    def tick(self, page) -> None:
        if self._fails >= self.MAX_TICK_FAILS:
            return
        self._fails = 0 if self._call(on_tick, page) else self._fails + 1
        if self._fails >= self.MAX_TICK_FAILS:
            self.ctx.log(f"[{self.ctx.account}] registration_hook.on_tick отключён: {self.MAX_TICK_FAILS} ошибок подряд")
