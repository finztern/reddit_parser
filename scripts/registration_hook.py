#!/usr/bin/env python3
"""
scripts/registration_hook.py — ТВОЯ логика автоматической регистрации аккаунта.

Хаб (`login_hub.py --register`) и `manual_login.py --register` сами делают всё остальное:
  * поднимают браузер с профилем слота (fingerprint, locale, timezone) через прокси слота;
  * генерируют ник и пароль (scripts/account_creds.py -> credentials/<account>.json, 0600);
  * открывают <base_url>/register и вызывают функции ниже;
  * каждые ~3 с проверяют, залогинен ли аккаунт; как только вход обнаружен — сохраняют cookies
    и storage_state, включают слот, обновляют credentials/<account>.json (status=logged_in).

Встроенная часть: ящик на mailgenerator.net (вторая вкладка) и снимки каждого шага (HTML + скриншот в
state/registration_dump/<account>/) — по ним пишутся селекторы. Капчу и код из письма вводишь сам в noVNC.
Свою логику пиши в местах «ТВОЙ КОД».

Дамп шагов: каждый новый шаг формы автоматически сохраняется в state/registration_dump/<account>/
(.html без значений полей и скриптов + .png) — пройди регистрацию руками и пришли эти файлы,
по ним пишутся селекторы. Перед отправкой посмотри .png: на скриншоте может быть твоя почта.

Правила:
  * `page` — синхронный Playwright Page. Не закрывай page/context/browser — это делает хаб.
  * Не блокируй надолго: on_tick вызывается в том же потоке, что и ожидание входа; пока он работает,
    вход не проверяется. Долгие шаги дроби на тики (храни прогресс в ctx.state).
  * Исключения не роняют сессию: они логируются (один раз на тип), после 20 подряд упавших
    on_tick хук отключается до конца сессии.
  * Не пиши пароль и куки в логи: ctx.log() идёт в консоль оператора.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_creds as ac  # noqa: E402


# JS: компактный снимок DOM для написания селекторов. Заходит в shadow DOM (форма Reddit — веб-компоненты),
# выбрасывает script/style/svg, не включает значения полей (пароль/почта не попадают в дамп).
DUMP_JS = r"""
() => {
  const KEEP = ['id','name','type','placeholder','aria-label','autocomplete','role','href','src','data-testid',
                'for','slot','disabled','required','inputmode','maxlength','pattern','action','method'];
  const SKIP = new Set(['SCRIPT','STYLE','SVG','NOSCRIPT','PATH','LINK','META']);
  function vis(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
  function ser(n, d) {
    if (d > 60) return '';
    if (n.nodeType === 3) { const t = n.textContent.trim().replace(/\s+/g, ' '); return t ? t.slice(0, 120) : ''; }
    if (n.nodeType !== 1 || SKIP.has(n.tagName.toUpperCase())) return '';
    const tag = n.tagName.toLowerCase();
    let a = '';
    for (const k of KEEP) { const v = n.getAttribute(k); if (v !== null) a += ` ${k}="${String(v).slice(0, 160).replace(/"/g, "'")}"`; }
    if (!vis(n) && !n.shadowRoot && tag !== 'html' && tag !== 'body') a += ' data-hidden="1"';
    let inner = '';
    if (n.shadowRoot) inner += '<shadow-root>' + Array.from(n.shadowRoot.childNodes).map(c => ser(c, d + 1)).join('') + '</shadow-root>';
    inner += Array.from(n.childNodes).map(c => ser(c, d + 1)).join('');
    return `<${tag}${a}>${inner}</${tag}>`;
  }
  return ser(document.documentElement, 0);
}
"""


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

    def dump(self, page, label: str = "step") -> Path | None:
        """Снимок текущего шага в state/registration_dump/<account>/ (.html без значений полей + .png)."""
        return dump_step(page, self.account, label)

    def save_creds(self, **fields) -> None:
        """Дописать поля (email, recovery и т.п.) в credentials/<account>.json."""
        cur = ac.load_credentials(self.account) or {"account": self.account, "username": self.username,
                                                    "password": self.password}
        cur.update(fields)
        ac.save_credentials(self.account, cur)


# --- настройки встроенной части --------------------------------------------------------------
MAILBOX_ENABLED = True            # создавать ящик на mailgenerator.net во второй вкладке
MAILBOX_URL = "https://mailgenerator.net"
MAX_DUMPS = 40                    # сколько автоснимков шагов сохранять за сессию
EMAIL_SELECTORS = ('input[type="email"]', 'input[name="email"]', "#register-email")


def dump_dir(account: str) -> Path:
    from scraper.identity import STATE_DIR
    return Path(STATE_DIR) / "registration_dump" / account


def dump_step(page, account: str, label: str = "step") -> Path | None:
    """Снимок текущего шага для написания селекторов: .html (DOM с shadow DOM и iframe, без значений полей и
    скриптов) + .png. Скриншот может показывать введённые данные — папка в state/ (в .gitignore)."""
    try:
        parts = [page.evaluate(DUMP_JS)]
        main = getattr(page, "main_frame", None)
        for fr in getattr(page, "frames", None) or []:
            if fr is not main:
                try:
                    parts.append(f"<iframe-dump src=\"{fr.url}\">{fr.evaluate(DUMP_JS)}</iframe-dump>")
                except Exception:  # noqa: BLE001 — кросс-доменный/закрытый фрейм
                    pass
        d = dump_dir(account)
        d.mkdir(parents=True, exist_ok=True)
        base = f"{len(list(d.glob('*.html'))) + 1:02d}-{re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:40]}"
        (d / f"{base}.html").write_text(f"<!-- {page.url} -->\n" + "\n".join(parts), encoding="utf-8")
        try:
            page.screenshot(path=str(d / f"{base}.png"))
        except Exception:  # noqa: BLE001 — скриншот не критичен
            pass
        return d / f"{base}.html"
    except Exception:  # noqa: BLE001
        return None


def create_mailbox(page, ctx: RegCtx) -> str | None:
    """Создаёт ящик на mailgenerator.net во ВТОРОЙ вкладке (тот же браузер и прокси слота). Публичного API у
    сервиса нет, поэтому через интерфейс; разметка не проверялась на живом сайте — при неудаче снимки
    `mailbox-*` в registration_dump покажут, что поправить. Код из письма вводишь сам (читаешь во второй вкладке)."""
    local = f"{ctx.username}{int(time.time()) % 1000}".lower().replace("-", "_")[:30]
    mailbox_pw = ac.make_password(14)
    mp = page.context.new_page()
    try:
        mp.goto(MAILBOX_URL, timeout=45000, wait_until="domcontentloaded")
        mp.wait_for_timeout(1500)
        for label in ("Accept", "Reject"):      # баннер cookies
            try:
                mp.get_by_role("button", name=re.compile(f"^{label}$", re.I)).first.click(timeout=1500)
                break
            except Exception:  # noqa: BLE001
                pass
        ctx.dump(mp, "mailbox-home")
        create_btn = mp.get_by_role("button", name=re.compile("create inbox", re.I)).first
        if not _visible(create_btn):
            opener = mp.get_by_text(re.compile(r"(new|create|get).{0,20}(mailbox|inbox|address)", re.I)).first
            opener.click(timeout=5000)
            mp.wait_for_timeout(800)
        create_btn.wait_for(state="visible", timeout=8000)
        box = create_btn.locator("xpath=ancestor::*[self::form or @role='dialog' or contains(@class,'modal')][1]")
        scope = box if box.count() else mp
        scope.locator("input:not([type=password]):not([type=hidden]):not([type=checkbox])").first.fill(local)
        scope.locator("input[type=password]").first.fill(mailbox_pw)
        ctx.dump(mp, "mailbox-filled")
        create_btn.click()
        create_btn.wait_for(state="hidden", timeout=15000)
        mp.wait_for_timeout(1000)
        ctx.dump(mp, "mailbox-created")
    except Exception as e:  # noqa: BLE001
        ctx.dump(mp, "mailbox-failed")
        ctx.log(f"[{ctx.account}] ящик на mailgenerator.net не создан автоматически: {type(e).__name__}: {e}")
        return None
    email = f"{local}@mailgenerator.net"
    ctx.save_creds(email=email, mailbox_password=mailbox_pw)
    return email


def _visible(loc) -> bool:
    try:
        return loc.count() > 0 and loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


def fill_email_once(page, ctx: RegCtx) -> bool:
    """Подставляет почту в ПУСТОЕ видимое поле e-mail формы Reddit (один раз)."""
    email = ctx.state.get("email")
    if not email or ctx.state.get("email_filled"):
        return False
    for sel in EMAIL_SELECTORS:
        loc = page.locator(sel).first
        if _visible(loc):
            if not loc.input_value():
                loc.fill(email)
            ctx.state["email_filled"] = True
            return True
    return False


def on_page_ready(page, ctx: RegCtx) -> None:
    """Вызывается ОДИН раз после page.goto(<base_url>/register).

    Встроенная часть: ящик на mailgenerator.net (вторая вкладка), возврат на вкладку
    регистрации. Ниже — место для твоей логики.
    """
    if MAILBOX_ENABLED:
        email = create_mailbox(page, ctx)
        page.bring_to_front()
        if email:
            ctx.state["email"] = email
            ctx.log(f"[{ctx.account}] почта: {email}  (письма: вторая вкладка браузера; код из письма вводишь сам)")
        else:
            ctx.log(f"[{ctx.account}] почту возьми вручную (mailgenerator.net) — снимки: {dump_dir(ctx.account)}")
    # ============================ ТВОЙ КОД: начало ============================
    # Сюда — шаги, которые нужно сделать один раз сразу после открытия формы.
    # ============================= ТВОЙ КОД: конец ============================


def on_tick(page, ctx: RegCtx) -> None:
    """Вызывается каждые ~3 с, пока вход не обнаружен (после on_page_ready).

    Встроенная часть: подстановка почты в пустое поле e-mail.
    Для многошаговых форм храни прогресс в ctx.state (`ctx.state["step"] = 2`).
    """
    fill_email_once(page, ctx)
    # ============================ ТВОЙ КОД: начало ============================
    # Сюда — логика шагов (ник/пароль: ctx.username / ctx.password), вызывается каждые ~3 с.
    # ============================= ТВОЙ КОД: конец ============================


# ---------------------------------------------------------------------------
# Дальше — обвязка, её менять не нужно.
# ---------------------------------------------------------------------------

class HookRunner:
    MAX_TICK_FAILS = 20

    def __init__(self, ctx: RegCtx):
        self.ctx, self._ready, self._fails, self._seen = ctx, False, 0, set()
        self._sig, self._dumps = None, 0

    def _autodump(self, page, label: str) -> None:
        """Снимок при каждой смене страницы/набора полей: ты проходишь шаги, файлы копятся сами."""
        if self._dumps >= MAX_DUMPS:
            return
        try:
            html = page.evaluate(DUMP_JS)
            sig = hashlib.sha1(f"{page.url}\n{html}".encode()).hexdigest()
        except Exception:  # noqa: BLE001
            return
        if sig == self._sig:
            return
        self._sig, self._dumps = sig, self._dumps + 1
        path = dump_step(page, self.ctx.account, label)
        if path:
            self.ctx.log(f"[{self.ctx.account}] снимок шага: {path}")

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
            self._autodump(page, "ready")
            self._call(on_page_ready, page)

    def tick(self, page) -> None:
        if self._fails >= self.MAX_TICK_FAILS:
            return
        self._autodump(page, "tick")
        self._fails = 0 if self._call(on_tick, page) else self._fails + 1
        if self._fails >= self.MAX_TICK_FAILS:
            self.ctx.log(f"[{self.ctx.account}] registration_hook.on_tick отключён: {self.MAX_TICK_FAILS} ошибок подряд")
