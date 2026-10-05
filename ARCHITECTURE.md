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
  свой proxy_port, cookies, impersonate, BackoffState, CookieFileWatcher
        ▲ слот выдаёт PollScheduler (по кругу, с джиттером)
        │
        └──► GET /r/all/comments.json ─► фильтр max_age ─► SeenCache
             ─► TokenBucket ─► POST /store_items (батч)

  Вне процесса (хост, systemd):
    refresh_cookies.py        (логин, account_1..10)   ─┐ пишут атомарно
    refresh_guest_cookies.py  (гости, account_11..25)  ─┤ cookies/account_N.json
    manual_login.py           (первый логин)           ─┘      │
                                           CookieFileWatcher ◄─┘ (hot-reload)
```

Все аккаунты опрашивают **один и тот же поток** (`stream_subs`, обычно
`r/all`), но не каждый по своему таймеру: единый `PollScheduler` раздаёт
«слоты» аккаунтам по кругу. Это убирает пачки одновременных запросов и
почти все дубли от пересекающихся опросов.

Общие объекты: `TokenBucket`, `SeenCache`, `PollScheduler`, `QueueGovernor`,
`ExecutorHandle`, `ConfigStore`. Своё на аккаунт: proxy-порт, cookies +
`CookieFileWatcher`, `impersonate`/UA, `curl_cffi.Session`
(`CurlSessionHandle`), `BackoffState`, слот планировщика.

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
останавливает (раньше `FIRST_COMPLETED` по всем задачам именно так и делал, и
docker перезапускал процесс по кругу).

### `scraper/scheduler.py` — `PollScheduler`
Раз в `interval` секунд (± `stream_jitter_ratio`) выбирает по кругу
аккаунта, который не занят и у которого `not_before` (его личный
rate-limit-кулдаун) наступил, и кладёт ему слот в очередь. `interval`
подстраивается по доле дублей на 1-й странице (EWMA): мало дублей → чаще
(можем пропускать комментарии), много → реже.

### `scraper/governor.py` — `QueueGovernor`
Читает `GET queue_control.url` (JSON: `bpipe_queue`, `bpipe_max`,
`upipe_queue`, `degraded`, `action`) и двигает потолок `TokenBucket`:
ускоряется, пока `bpipe_queue < target_min * headroom_ratio`, тормозит выше
`target_high`, резко — при `degraded`/почти полных очередях. Если эндпоинт
молчит дольше `stale_seconds` — fallback на `target_rate_per_second`. URL
можно переопределить env `QUEUE_CONTROL_URL`.

### `scraper/worker.py` — `account_worker`
1. Если `cookies/account_N.json` нет — ждёт его появления (раз в минуту);
   так гостевой слот можно включить до первого харвеста.
2. Регистрирует слот и в цикле ждёт слот от планировщика.
3. На каждом слоте: читает актуальный конфиг; `CookieFileWatcher` (по mtime,
   не чаще `cookie_reload_check_interval_seconds`) — при изменении файла
   обновляет `session.cookie_jar`, пересоздаёт `curl_cffi`-сессию (она копит
   `Set-Cookie`, старые значения не должны конфликтовать с новыми) и сбрасывает
   счётчик auth-ошибок.
4. `fetch_comments(..., stop_when=…)`: страницы листаются, пока не встретится
   уже виденный комментарий («догнали»), страница не станет старше `max_age`,
   не кончится `after` или не будет достигнут лимит страниц.
5. Ошибки: `rate_or_server` (429/5xx) → экспоненциальный кулдаун аккаунта
   (+`Retry-After`); `network` → мягкий кулдаун ≤30 с; `auth` (401/403) →
   кулдаун, а после `max_consecutive_auth_errors` подряд — **пауза с редкими
   пробами** (`auth_dead_cooldown_seconds`), а не завершение воркера. Новые
   cookies от refresh-джоба сами выводят воркер из паузы.
6. Успех: `build_payload` → фильтр `max_age` → сортировка по свежести →
   `SeenCache.try_claim` → токен `TokenBucket` → батч в `/store_items` →
   `confirm()`/`release()` по каждому элементу (с `finally`-страховкой).
7. `cooldown` аккаунта = `reset / (remaining * ratelimit_safety_margin)` по
   `X-Ratelimit-*`, максимум `ratelimit_max_interval_seconds`; передаётся в
   `scheduler.release()`.

`supervised()` перезапускает воркер при исключениях (экспоненциальная
задержка до 60 с); штатный `return` не перезапускает.

### `scraper/http_client.py`
`_fetch_comments_page` — один запрос `curl_cffi` в `http_executor` под
`asyncio.wait_for(connect + read + slack)`; cookies берутся из
`session.cookie_jar` на каждый запрос. `fetch_comments` — пагинация.
`pagination_max_pages <= 0` = «без лимита» (≤10 страниц; раньше 0
останавливал листание сразу после первой страницы).

### `scraper/health.py`
`ExecutorHandle` пересоздаёт пул при зависании потоков `curl_cffi`
(`wait_for` не убивает физический поток). `ExecutorHealth` считает занятость
текущего пула; у каждого пула своё **поколение**, завершения задач старых
пулов игнорируются (раньше они уводили `active` в минус и ломали следующий
своп). Предохранители: `swap_cooldown`, `fatal_leak_multiplier` → `SystemExit`
(docker перезапустит).

### `scraper/state.py`
`TokenBucket`, `SeenCache` (двухфазный `try_claim → confirm/release`;
элемент считается виденным только после подтверждённой отправки),
`BackoffState`.

### `scraper/pipeline.py`
`build_payload` (схема коллектора: `content`, `external_id`, `created_at`,
`external_parent_id` для `t1_`/`t3_`, `summary` и т.д.) и
`send_batch_to_store` (чанки по `min(batch_max_items, 1000)`, поэлементные
результаты, 413/ошибки → весь чанк `False`).

### `scraper/config.py`
`ConfigStore` (hot-reload), `load_accounts`, `load_cookies`,
`CookieFileWatcher`.

## 3. Блокирующий `curl_cffi` и пул потоков

`curl_cffi` синхронный → `run_in_executor`. `wait_for` на таймауте отменяет
только asyncio-обёртку, поток остаётся занят. Меры: запас потоков, раздельные
connect/read-таймауты, `wait_for` с запасом, пересоздание `curl`-сессии
аккаунта при таймауте, и своп всего пула (`health.py`).

## 4. Дедуп и надёжность доставки

Любой сбой на пути (HTTP-ответ, таймаут, исключение, отмена задачи) →
`release()`: элемент будет подхвачен на следующем опросе, пока не протух по
`max_age_seconds`. Если пайплайн недоступен дольше `max_age_seconds`, часть
комментариев теряется — осознанный компромисс свежести и доставки.

## 5. Конфигурация и hot-reload

`config.yaml` — каждые `CONFIG_RELOAD_SECONDS` (5 с). `accounts.yaml` — один
раз при старте. Файл cookies конкретного аккаунта — на лету
(`CookieFileWatcher`). Env: `CONFIG_PATH`, `ACCOUNTS_PATH`, `PROXY_HOST`,
`STORE_ENDPOINT`, `QUEUE_CONTROL_URL`, `CONFIG_RELOAD_SECONDS`, `LOG_FILE`,
`EXECUTOR_*`.

## 6. Внешние джобы (cookies)

Единственная точка пересечения с процессом скрапера — файл
`cookies/account_N.json`. Джобы пишут его атомарно (`os.replace()` из
tmp-файла в той же директории), поэтому воркер всегда видит целый файл.

- **`refresh_cookies.py`** — логин-аккаунты (`login_accounts`). Браузерный
  движок выводится из `impersonate` (firefox → firefox, chrome → chromium,
  safari → webkit); контекст идёт через тот же `proxy_port`; залогиненность —
  по позитивному признаку (`/api/me.json` → `data.name`), при её отсутствии
  файл не трогается. Пишутся только cookies домена reddit.com.
- **`refresh_guest_cookies.py`** — гостевые слоты (`guest_accounts`).
  `impersonate`/UA берутся из `accounts.yaml`; харвест → верификация
  `comments.json` тем же `curl_cffi`-отпечатком → запись только при 200;
  повторный проход по сорвавшимся слотам.
- **`manual_login.py`** — первичный логин через Xvfb + x11vnc + noVNC.
- Один браузер за раз: последовательный цикл внутри джоба + общий `flock` в
  systemd-юнитах между джобами.

Прокси-порт у джобов и скрапера **один и тот же** (`proxy_port` из
`accounts.yaml`) — иначе Reddit увидит одну сессию с двух IP.

## 7. Случайность (анти-детект)

1. Фаза слотов: единый планировщик + `stream_jitter_ratio`.
2. Паузы между страницами пагинации (`pagination_delay_*`).
3. Личный кулдаун аккаунта по `X-Ratelimit-*` (адаптивный, не «круглый»).
4. Разные `impersonate`+UA на аккаунт; гео браузера джобов = гео ноды.
5. `stagger_seconds` между аккаунтами в refresh-джобах.
