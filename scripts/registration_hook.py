#!/usr/bin/env python3
"""
scripts/registration_hook.py — ТВОЯ логика автоматической регистрации аккаунта.

Хаб (`login_hub.py --register`) и `manual_login.py --register` сами делают всё остальное:
  * поднимают браузер с профилем слота (fingerprint, locale, timezone) через прокси слота;
  * генерируют ник и пароль (scripts/account_creds.py -> credentials/<account>.json, 0600);
  * открывают <base_url>/register и вызывают функции ниже;
  * каждые ~3 с проверяют, залогинен ли аккаунт; как только вход обнаружен — сохраняют cookies
    и storage_state, включают слот, обновляют credentials/<account>.json (status=logged_in).

Встроенная часть: адрес ящика с mailgenerator.net (вторая вкладка), автозаполнение шагов формы и снимки каждого шага (HTML + скриншот в
state/registration_dump/<account>/) — по ним пишутся селекторы. Капчу решаешь сам в noVNC; код из письма хук подставляет и подтверждает Enter.
Свою логику пиши в местах «ТВОЙ КОД».

Дамп шагов: каждый новый шаг формы автоматически сохраняется в state/registration_dump/<account>/
(.html без значений полей и скриптов + .png) — по ним пишутся и чинятся селекторы.
Перед отправкой посмотри .png: на скриншоте может быть твоя почта.

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
    link: str = ""          # ссылка noVNC этого аккаунта (хаб): печатается, когда нужно твоё нажатие Enter

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
VERBOSE = False                   # True — печатать каждый снимок и смену шага (по умолчанию в консоли только важное)
MAX_DUMPS = 40                    # сколько автоснимков шагов сохранять за сессию
CLICK_RETRY_S = 20.0              # повтор нажатия одной и той же кнопки не чаще (кнопка может ждать капчу)
_now = time.monotonic

# Селекторы шагов формы Reddit (из снимков реальной регистрации; shadow DOM пробивается автоматически).
EMAIL_INPUT = 'faceplate-text-input[name="email"] input'
EMAIL_SUBMIT = 'auth-flow-modal[slot="register_email"] [slot="primaryButton"] button'
CODE_INPUT = 'faceplate-text-input[name="code"] input'
USERNAME_INPUT = "#register-username input"
PASSWORD_INPUT = "#register-password input"
USERNAME_SUBMIT = 'auth-flow-modal[slot="register_username_and_password"] button[type="submit"]'
# Ошибка «Beim Senden des Verifizierungscodes ist etwas schiefgelaufen» после «Продолжить»: баннер внутри
# модалки шага почты (язык-независимо) либо текст по словам (de/en/ru).
EMAIL_ERROR_BANNER = ('auth-flow-modal[slot="register_email"] :is(rpl-info-banner, inline-banner, [role="alert"])')
SEND_ERROR_RE = re.compile(r"Verifizierungscode|verification code|код подтверждения|schiefgelaufen|went wrong|"
                           r"что-то пошло не так", re.I)
EMAIL_MAX_TRIES = 3               # попыток отправить код (первая + повторы), потом смена ноды и профиля
STUCK_S = 15.0                    # форма почты не сменилась на «код» за столько секунд после клика = неудача
ERROR_WAIT_S = 6.0                # через сколько секунд после «Продолжить» ошибку считаем ответом на ЭТУ попытку
AGE_INPUTS = {k: f'faceplate-text-input[name="{k}"] input' for k in ("day", "month", "year")}
AGE_SUBMIT = "#age-collect-submit-btn"
AGE_CONFIRM = 'auth-flow-modal[slot="onboarding_age_confirmation"] button[type="submit"]'


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


def get_mailbox(page, ctx: RegCtx) -> str | None:
    """Читает адрес ящика с mailgenerator.net во ВТОРОЙ вкладке (тот же браузер и прокси слота). Сайт сам
    создаёт ящик при заходе — ничего создавать не нужно: адрес лежит в `#activeEmail`, пароль ящика (для
    возврата в него позже) — в `#loginWrap` («Password: …»). Вкладку не закрываем: в ней приходят письма,
    код из письма ты вводишь сам. При неудаче снимок `mailbox-failed` в registration_dump."""
    mp = page.context.new_page()
    ctx.state["mail_page"] = mp
    try:
        mp.goto(MAILBOX_URL, timeout=45000, wait_until="domcontentloaded")
        try:
            mp.locator("#cookieRejectBtn").click(timeout=2500)      # баннер cookies
        except Exception:  # noqa: BLE001
            pass
        email = ""
        for _ in range(40):                                            # адрес появляется скриптом, до ~20 с
            email = (mp.locator("#activeEmail").first.text_content() or "").strip()
            if "@" in email:
                break
            mp.wait_for_timeout(500)
        if "@" not in email:
            raise RuntimeError("адрес не появился в #activeEmail")
        ctx.dump(mp, "mailbox")
        fields = {"email": email}
        try:
            m = re.search(r"Password:\s*(\S+)", mp.locator("#loginWrap").first.text_content(timeout=2000) or "")
            if m:
                fields["mailbox_password"] = m.group(1)
        except Exception:  # noqa: BLE001
            pass
        ctx.save_creds(**fields)
        return email
    except Exception as e:  # noqa: BLE001
        ctx.dump(mp, "mailbox-failed")
        ctx.log(f"[{ctx.account}] почта с mailgenerator.net не прочитана: {type(e).__name__}: {e}")
        return None


def _visible(loc) -> bool:
    try:
        return loc.count() > 0 and loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _shown(page, sel):
    loc = page.locator(sel).first
    return loc if _visible(loc) else None


def _fill_empty(loc, value: str) -> None:
    if not loc.input_value():
        loc.fill(value)


def _click_once(page, ctx: RegCtx, key: str, sel: str) -> bool:
    """Нажимает кнопку, если она видна и активна; повтор не чаще раза в CLICK_RETRY_S (кнопка может ждать капчу)."""
    btn = _shown(page, sel)
    if btn is None or not btn.is_enabled():
        return False
    clicked = ctx.state.setdefault("clicked", {})
    now = _now()
    if now - clicked.get(key, -1e9) < CLICK_RETRY_S:
        return False
    clicked[key] = now
    btn.click()
    return True


def send_error_visible(page) -> bool:
    """Reddit не смог отправить код на почту (ошибка под формой e-mail)."""
    banner = _shown(page, EMAIL_ERROR_BANNER)
    if banner is not None:
        return True
    try:
        return _visible(page.get_by_text(SEND_ERROR_RE).first)
    except Exception:  # noqa: BLE001
        return False


def email_step(page, ctx: RegCtx, email_input) -> str:
    """Почта -> «Продолжить». Если Reddit ответил ошибкой отправки кода — повтор через пару секунд; после
    EMAIL_MAX_TRIES неудач ставит ctx.state["replace_identity"] (хаб меняет ноду и профиль и начинает заново)."""
    _fill_empty(email_input, ctx.state["email"])
    t = _now()
    last = ctx.state.get("email_click_at")
    # неудача = ошибка на экране через ERROR_WAIT_S ИЛИ форма почты всё ещё на месте через STUCK_S после клика
    # (на случай, если текст/баннер ошибки не распознан: код приходит за секунды, шаг сменяется на «код»)
    failed = last is not None and (t - last >= STUCK_S or (t - last >= ERROR_WAIT_S and send_error_visible(page)))
    if failed:
        ctx.state["email_fails"] = ctx.state.get("email_fails", 0) + 1
        n = ctx.state["email_fails"]
        if n >= EMAIL_MAX_TRIES:
            ctx.state["replace_identity"] = "reddit не отправляет код (ошибка после %d попыток)" % n
            ctx.log(f"[{ctx.account}] код не отправлен {n} раз подряд — меняю ноду и профиль")
            return "email_failed"
        ctx.log(f"[{ctx.account}] код не отправлен (попытка {n} из {EMAIL_MAX_TRIES}) — пробую ещё раз")
        ctx.state.setdefault("clicked", {}).pop("email", None)       # разрешить немедленный повтор
        ctx.state["email_click_at"] = None
    if _click_once(page, ctx, "email", EMAIL_SUBMIT):
        ctx.state["email_click_at"] = _now()
    return "email"


COOKIE_BTN_RE = re.compile(r"Optionale Cookies|Nicht (unbedingt )?notwendige|Reject non-essential|Reject optional|"
                           r"Отклонить необязательные", re.I)


def dismiss_cookies(page, ctx: RegCtx) -> bool:
    """Баннер cookies («Optionale Cookies ablehnen» / «Reject non-essential») — отклоняем необязательные,
    чтобы он не перекрывал форму и «живую» сессию. Не чаще раза в 10 с."""
    now = _now()
    if now - ctx.state.get("cookie_try", -1e9) < 10.0:
        return False
    try:
        btn = page.get_by_role("button", name=COOKIE_BTN_RE).first
        if not _visible(btn):
            return False
        ctx.state["cookie_try"] = now
        btn.click(timeout=3000)
        return True
    except Exception:  # noqa: BLE001
        return False


LOGIN_BTN_RE = re.compile(r"^\s*(Log ?In|Anmelden|Einloggen|Войти)\s*$", re.I)
SIGNUP_BTN_RE = re.compile(r"^\s*(Sign ?up|Registrieren|Zarejestruj się|Зарегистрироваться)\s*$", re.I)


def _click_named(page, rx) -> bool:
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=rx).first
            if _visible(loc):
                loc.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def open_signup(page, ctx: RegCtx) -> str | None:
    """Путь к форме регистрации с главной reddit.com: синяя кнопка «Log In» (если есть) -> в окне входа
    «Sign up». Не чаще раза в 5 с; работает, пока форма регистрации ещё не появилась."""
    now = _now()
    if now - ctx.state.get("open_try", -1e9) < 5.0:
        return None
    ctx.state["open_try"] = now
    if _click_named(page, SIGNUP_BTN_RE):
        return "open_signup"
    if _click_named(page, LOGIN_BTN_RE):
        return "open_login"
    return None


def make_birthdate(rng=None) -> dict:
    """Случайная взрослая дата рождения (возраст 22–38, день ≤ 28 — подходит любому месяцу)."""
    import secrets
    rng = rng or secrets.SystemRandom()
    year = time.gmtime().tm_year - rng.randint(22, 38)
    return {"day": rng.randint(1, 28), "month": rng.randint(1, 12), "year": year}


CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
MAIL_RELOAD_S = 20.0              # как часто перезагружать вкладку почты, если письма всё нет


def read_mail_code(ctx: RegCtx) -> str | None:
    """Ищет 6-значный код в письме от Reddit во вкладке mailgenerator.net (список `#messageList`; если код не
    виден в списке — открывает письмо и читает `#modalBody`). Ничего не вводит: код печатается оператору."""
    mp = ctx.state.get("mail_page")
    if mp is None:
        return None
    try:
        lst = (mp.locator("#messageList").first.inner_text(timeout=2000) or "")
        m = CODE_RE.search(lst)
        if m:
            return m.group(1)
        if "reddit" in lst.lower():
            mp.locator("#messageList").get_by_text(re.compile("reddit", re.I)).first.click(timeout=2000)
            body = mp.locator("#modalBody").first.inner_text(timeout=3000) or ""
            try:
                mp.locator("#modalClose").first.click(timeout=1500)
            except Exception:  # noqa: BLE001
                pass
            m = CODE_RE.search(body)
            return m.group(1) if m else None
        now = _now()
        if now - ctx.state.setdefault("mail_reload", now) >= MAIL_RELOAD_S:
            ctx.state["mail_reload"] = now
            mp.reload(wait_until="domcontentloaded", timeout=20000)
    except Exception:  # noqa: BLE001 — письмо ещё не пришло / вкладка перерисовывается: повторим на след. тике
        pass
    return None


def signup_step(page, ctx: RegCtx) -> str | None:
    """Определяет текущий шаг формы Reddit и делает безопасное автозаполнение. Селекторы взяты из снимков
    реальной регистрации (shadow DOM пробивается селекторами Playwright, привязка к slot/id, не к тексту).
    Код из письма хук ПОДСТАВЛЯЕТ в поле и сам жмёт Enter (в консоль — одна строка со ссылкой на окно);
    капчу решаешь ты. -> имя шага или None."""
    if ctx.state.get("replace_identity"):
        return "email_failed"
    dismiss_cookies(page, ctx)
    step = _signup_step(page, ctx)
    if step and not step.startswith("open_"):
        ctx.state["form_seen"] = True
    return step


def _signup_step(page, ctx: RegCtx) -> str | None:
    code_input = _shown(page, CODE_INPUT)
    if code_input is not None:
        code = read_mail_code(ctx)
        if code and code != ctx.state.get("code_printed"):
            ctx.state["code_printed"] = code
            code_input.fill(code)
            page.bring_to_front()
            code_input.click()
            try:
                code_input.press("Enter")
            except Exception:  # noqa: BLE001
                try:
                    page.keyboard.press("Enter")
                except Exception:  # noqa: BLE001
                    pass
            where = f"  {ctx.link}" if ctx.link else ""
            ctx.log(f">>> {ctx.account}: код подставлен ({code}) — регистрация продолжается автоматически{where}")
        return "code"
    name = _shown(page, USERNAME_INPUT)
    if name is not None:
        _fill_empty(name, ctx.username)
        _fill_empty(page.locator(PASSWORD_INPUT).first, ctx.password)
        _click_once(page, ctx, "username_password", USERNAME_SUBMIT)
        return "username_password"
    day = _shown(page, AGE_INPUTS["day"])
    if day is not None:
        bd = ctx.state.get("birthdate") or ctx.state.setdefault("birthdate", make_birthdate())
        ctx.save_creds(birthdate=bd)
        for key in ("day", "month", "year"):
            _fill_empty(page.locator(AGE_INPUTS[key]).first, str(bd[key]))
        _click_once(page, ctx, "age", AGE_SUBMIT)
        return "age"
    if _click_once(page, ctx, "age_confirm", AGE_CONFIRM):
        return "age_confirm"
    email = _shown(page, EMAIL_INPUT)
    if email is not None:
        if not ctx.state.get("email"):
            return "email_wait"
        return email_step(page, ctx, email)
    if not ctx.state.get("form_seen"):
        return open_signup(page, ctx)
    return None


def on_page_ready(page, ctx: RegCtx) -> None:
    """Вызывается ОДИН раз после page.goto(<base_url>/) (главная; к форме регистрации хук проходит сам).

    Встроенная часть: адрес ящика с mailgenerator.net (вторая вкладка), возврат на вкладку
    регистрации. Ниже - место для твоей логики.
    """
    if MAILBOX_ENABLED:
        email = get_mailbox(page, ctx)
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

    Встроенная часть: signup_step — почта → (код: вручную) → ник/пароль → дата рождения. Последующие
    шаги (пол, интересы, капча) — вручную. Для своих шагов храни прогресс в ctx.state.
    """
    step = signup_step(page, ctx)
    if step and step != ctx.state.get("last_step"):
        ctx.state["last_step"] = step
        if VERBOSE:
            ctx.log(f"[{ctx.account}] шаг регистрации: {step}")
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
        if path and VERBOSE:
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
