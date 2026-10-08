# Архитектура

Документ описывает скрапер **по коду в репозитории** (`main.py` + `scraper/`).

## 1. Общая картина

```
                    ┌────────────────────────────────────────┐
                    │                main.py                  │
                    │ ConfigStore (hot-reload)                │
                    │ TokenBucket  — потолок отправки         │
                    │ SeenCache    — дедуп                    │
                    │ PollScheduler— слоты опроса             │
                    │ QueueGovernor— ведёт потолок TokenBucket│
                    │ ExecutorHandle/Health — пул curl_cffi   │
                    │ aiohttp.ClientSession (store_items)     │
                    └───────────────┬────────────────────────┘
                                    │ N задач (supervised)
        ┌───────────────┬───────────┴───────┬───────────────┐
  account_worker   account_worker      account_worker   ...
  (account_1)      (account_2)         (account_N)
  свой proxy_port, cookies, impersonate, BackoffState, CookieFileWatcher,
  SlotProfileWatcher
        ▲ слот выдаёт PollScheduler (по кругу, с джиттером)
        │
        └──► GET /r/all/comments.json ─► фильтр max_age ─► SeenCache
             ─► TokenBucket ─► POST /store_items (батч)

  Вне процесса (хост, systemd):
    refresh_cookies.py        (логин, account_1..10)   ─┐ пишут атомарно
    refresh_guest_cookies.py  (гости, account_11..25)  ─┤ cookies/account_N.json
    manual_login.py           (первый логин)           ─┘      │
    rotation_daemon.py        (смена идентичности)  ──────────►│
                                           CookieFileWatcher ◄─┘ (hot-reload)
    state/requests, state/slots  ◄──► воркер (заявки / активный профиль)
```

Все аккаунты опрашивают **один и тот же поток** (`stream_subs`, обычно
`r/all`), но не каждый по своему таймеру: единый `PollScheduler` раздаёт
«слоты» аккаунтам по кругу. Это убирает пачки одновременных запросов и
почти все дубли от пересекающихся опросов.

Общие объекты: `TokenBucket`, `SeenCache`, `PollScheduler`, `QueueGovernor`,
`ExecutorHandle`, `ConfigStore`. Своё на аккаунт: proxy-порт, cookies +
`CookieFileWatcher`, `impersonate`/UA (+ `SlotProfileWatcher`),
`curl_cffi.Session` (`CurlSessionHandle`), `BackoffState`, слот планировщика.

Аккаунты 1..10 — логин, 11..25 — гостевые: для самого скрапера разницы нет
(оба читают `cookies/account_N.json`), отличается только джоб, который этот
файл обновляет.

## 2. Модули

### `main.py`
Поднимает общие объекты, `QueueGovernor`, `PollScheduler`, health-цикл и по
задаче `account_worker` на каждый `enabled: true` аккаунт (через
`supervised()`). Пул `ThreadPoolExecutor` — `max(32, 2 * аккаунтов)`.

**Остановка.** Процесс завершается только: по SIGTERM/SIGINT, при падении
любой «ядерной» задачи (config_reload, governor, health, scheduler, stats) или
когда завершились **все** воркеры. Завершение одного воркера процесс не
останавливает.

### `scraper/scheduler.py` — `PollScheduler`
Раз в `interval` секунд (± `stream_jitter_ratio`) выбирает по кругу
аккаунта, который не занят и у которого `not_before` (его личный
rate-limit-кулдаун) наступил, и кладёт ему слот в очередь. `interval`
подстраивается по доле дублей на 1-й странице (EWMA).

### `scraper/governor.py` — `QueueGovernor`
Читает `GET queue_control.url` и двигает потолок `TokenBucket`. Если эндпоинт
молчит дольше `stale_seconds` — fallback на `target_rate_per_second`.

### `scraper/worker.py` — `account_worker`
1. Если `cookies/account_N.json` нет — ждёт его появления (раз в минуту).
2. Читает активный профиль слота `state/slots/account_N.json` (если есть) —
   он перекрывает `impersonate`/UA из `accounts.yaml`. Регистрирует слот в
   планировщике.
3. На каждом слоте: проверяет заявку на ротацию (pending/working — слот
   пропускается, Reddit не опрашивается); читает актуальный конфиг;
   `SlotProfileWatcher` — при смене профиля меняет `impersonate`
   (`CurlSessionHandle.set_impersonate`); `CookieFileWatcher` — при смене
   файла обновляет cookies и пересоздаёт `curl_cffi`-сессию.
4. `fetch_comments(..., stop_when=…)`: страницы листаются, пока не встретится
   уже виденный комментарий, страница не станет старше `max_age`, не кончится
   `after` или не будет достигнут лимит страниц.
5. Ошибки: `rate_or_server` (429/5xx) → экспоненциальный кулдаун; `network`
   → мягкий кулдаун ≤30 с; `auth` (401/403) → кулдаун, после
   `max_consecutive_auth_errors` подряд — пауза с редкими пробами;
   `empty` (пустой листинг) → растущий кулдаун, а при
   `rotation.empty_streak_trigger` подряд — заявка на ротацию.
6. Успех: `build_payload` → фильтр `max_age` → `SeenCache.try_claim` →
   токен `TokenBucket` → батч в `/store_items` → `confirm()`/`release()`.
7. `cooldown` аккаунта = `reset / (remaining * ratelimit_safety_margin)`.

`supervised()` перезапускает воркер при исключениях (экспоненциальная
задержка до 60 с); штатный `return` не перезапускает.

### `scraper/identity.py`
Обмен состоянием с демоном ротации через `state/` (атомарная запись):
`requests/account_N.json` (заявка и её результат), `slots/account_N.json`
(активный профиль слота), `SlotProfileWatcher`.

### `scraper/http_client.py`
`_fetch_comments_page` — один запрос `curl_cffi` в `http_executor` под
`asyncio.wait_for`; `fetch_comments` — пагинация. `CurlSessionHandle` —
сессия аккаунта, `swap()` / `set_impersonate()`.

### `scraper/health.py`
`ExecutorHandle` пересоздаёт пул при зависании потоков `curl_cffi`;
`ExecutorHealth` считает занятость текущего пула по поколениям.

### `scraper/state.py`
`TokenBucket`, `SeenCache` (двухфазный `try_claim → confirm/release`),
`BackoffState`.

### `scraper/pipeline.py`
`build_payload` (схема коллектора) и `send_batch_to_store` (чанки по
`min(batch_max_items, 1000)`, поэлементные результаты).

### `scraper/config.py`
`ConfigStore` (hot-reload), `load_accounts`, `load_cookies`,
`CookieFileWatcher`.

## 3. Блокирующий `curl_cffi` и пул потоков

`curl_cffi` синхронный → `run_in_executor`. `wait_for` на таймауте отменяет
только asyncio-обёртку, поток остаётся занят. Меры: запас потоков, раздельные
connect/read-таймауты, пересоздание `curl`-сессии аккаунта при таймауте, и
своп всего пула (`health.py`).

## 4. Дедуп и надёжность доставки

Любой сбой на пути → `release()`: элемент будет подхвачен на следующем
опросе, пока не протух по `max_age_seconds`.

## 5. Конфигурация и hot-reload

`config.yaml` — каждые `CONFIG_RELOAD_SECONDS` (5 с). `accounts.yaml` — один
раз при старте. Файл cookies конкретного аккаунта и профиль слота — на лету.
Env: `CONFIG_PATH`, `ACCOUNTS_PATH`, `PROXY_HOST`, `STORE_ENDPOINT`,
`QUEUE_CONTROL_URL`, `CONFIG_RELOAD_SECONDS`, `STATE_DIR`, `LOG_FILE`,
`EXECUTOR_*`.

## 6. Внешние джобы (cookies)

Точки пересечения с процессом скрапера — `cookies/account_N.json` и `state/`.
Файлы пишутся атомарно (`os.replace()` из tmp в той же директории).

- **`refresh_cookies.py`** — логин-аккаунты.
- **`refresh_guest_cookies.py`** — гостевые слоты: харвест → верификация →
  запись только при 200. Слоты в ротации пропускает, активный профиль после
  ротации перекрывает `accounts.yaml`.
- **`manual_login.py`** — первичный логин через Xvfb + x11vnc + noVNC.
- Один браузер за раз: общий `flock` (`/tmp/reddit_scraper_browser.lock`).

Прокси-порт у джобов и скрапера **один и тот же** (`proxy_port`).

## 7. Ротация идентичностей (`scripts/rotation_daemon.py`)

Идентичность = нода + отпечаток + гео (cookies — отдельно, харвестятся
заново). Реестр — `state/identities.json`: у каждой идентичности `banned_at`,
у слота — активная идентичность и история ротаций.

Поток: воркер → заявка `pending` → демон (под browser-lock): ставит бан
текущей идентичности, выбирает кандидата (целинные ноды с новым отпечатком →
отлежавшиеся забаненные, самая старая первой), переключает ноду в mihomo
(`PUT /proxies/pg-account_N`), проверяет живость, харвестит и верифицирует
cookies, затем пишет `state/slots/account_N.json` и подкладывает cookies,
после чего `status=done`. При неудаче — откат ноды и `rejected` с
`retry_after`.

Защиты: лимит ротаций в сутки на слот; массовые заявки (≥ доли слотов) —
считаются сбоем Reddit/сети, а не баном; ноды логин-аккаунтов не отдаются;
одна нода — один слот. Каждые `reconcile_seconds` демон сверяет выбор ноды в
mihomo с реестром (после рестарта mihomo `select` сбрасывается).
Переживает рестарты контейнеров и демона.

## 8. Случайность (анти-детект)

1. Фаза слотов: единый планировщик + `stream_jitter_ratio`.
2. Паузы между страницами пагинации (`pagination_delay_*`).
3. Личный кулдаун аккаунта по `X-Ratelimit-*`.
4. Разные `impersonate`+UA на аккаунт; гео браузера джобов = гео ноды.
5. `stagger_seconds` между аккаунтами в refresh-джобах.
