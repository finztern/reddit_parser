# Reddit fresh-comments scraper

Опрашивает общий поток комментариев (`www.reddit.com/r/all/comments.json`,
см. `stream_subs`) с нескольких аккаунтов, каждый — через свой VPN-выход
(своя нода VLESS через mihomo), и льёт свежие комментарии в локальный
пайплайн (`POST /store_items`, батчами). Скорость отправки сама
подстраивается под очередь пайплайна.

Внутреннее устройство — в [ARCHITECTURE.md](./ARCHITECTURE.md).

## Аккаунты: 10 логин + 15 гостевых

| Слоты | Тип | Откуда cookies | Кто обновляет |
|---|---|---|---|
| `account_1..10` | **логин** (реальные аккаунты Reddit) | первый логин руками (`manual_login.py`) | `scripts/refresh_cookies.py`, раз в неделю |
| `account_11..25` | **гостевые** (анонимная сессия, без логина) | харвест браузером | `scripts/refresh_guest_cookies.py`, раз в час |

Правила, которые нельзя нарушать (оба джоба пишут в один и тот же
`cookies/account_N.json`):

- `refresh_cookies.py` работает только с `login_accounts` из
  `scripts/refresh_cookies.yaml` (1..10);
- `refresh_guest_cookies.py` работает только со слотами из `guest_accounts`
  в `scripts/refresh_guest_cookies.yaml` (11..25);
- **источник правды для `impersonate`/`user_agent` — `accounts.yaml`.** Именно
  с этим TLS-отпечатком скрапер ходит за `comments.json`; браузер-джобы
  берут отпечаток оттуда же (при расхождении — WARNING). Менять отпечатки
  — через `scripts/rotate_fingerprints.py`, он правит оба файла синхронно.

Гостевые слоты `enabled: false`, пока для них нет ноды в
`vless_accounts.env` (строки `account_11 = vless://...` … `account_25 = ...`)
и не отработал первый харвест. Если слот включён, а cookies-файла ещё нет,
воркер ждёт его появления (раз в минуту), а не падает.

## Структура

```
.
├── main.py                     # точка входа
├── scraper/
│   ├── constants.py            # пути, таймауты, логгер, env-переопределения
│   ├── config.py               # ConfigStore (hot-reload config.yaml), CookieFileWatcher
│   ├── models.py               # FetchResult
│   ├── http_client.py          # запросы к Reddit (curl_cffi) + пагинация
│   ├── pipeline.py             # payload комментария + батч-отправка в /store_items
│   ├── state.py                # TokenBucket, SeenCache, BackoffState
│   ├── scheduler.py            # PollScheduler: единые слоты опроса по кругу
│   ├── governor.py             # QueueGovernor: скорость отправки по очереди пайплайна
│   ├── health.py               # мониторинг/своп http_executor
│   └── worker.py               # account_worker, supervised()
├── scripts/
│   ├── refresh_cookies.py / .yaml          # логин-аккаунты 1..10
│   ├── refresh_guest_cookies.py / .yaml    # гостевые слоты 11..25
│   ├── manual_login.py         # первый ручной логин (Xvfb + noVNC)
│   ├── generate_mihomo_config.py           # mihomo_config.yaml из vless_accounts.env
│   ├── rotate_fingerprints.py  # смена impersonate+UA (правит accounts.yaml + guest yaml)
│   ├── rotation.py             # смена ноды/отпечатка на лету (библиотека)
│   ├── check_nodes.py          # диагностика цепочки mihomo -> нода -> Reddit
│   ├── test_curl_guest.py      # проверка гостевых cookies БЕЗ браузера (curl_cffi)
│   └── systemd/                # timer/service джобов
├── config.yaml                 # тюнинг (hot-reload)
├── accounts.yaml               # 25 слотов (читается при старте)
├── vless_accounts.env          # account_N = vless://... (секреты, в .gitignore)
├── mihomo_config.yaml          # СГЕНЕРИРОВАН — не редактируй руками
├── cookies/account_N.json      # cookies (Cookie Editor JSON)
├── storage_state/account_N.json# сессии Playwright (логин-аккаунты)
├── requirements.txt            # зависимости скрапера (в образ)
├── requirements-refresh.txt    # зависимости джобов (playwright, curl_cffi)
├── Dockerfile
├── docker-compose.yml          # Linux (network_mode: host)
└── docker-compose.bridge.yml   # Mac/Windows
```

## Установка

### 1. venv для джобов (на хосте, не в контейнере)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-refresh.txt
venv/bin/playwright install --with-deps chromium firefox webkit
# webkit обязателен: гостевые слоты 12,14,15,17,18,20,21 — Safari/iOS-профили.
# firefox нужен логин-аккаунтам 1,2,5,10 и гостю 23 (движок выводится из impersonate).
```

### 2. Ноды

`vless_accounts.env` — по строке на слот (для 1..10 уже есть, для 11..25
допиши):

```
account_11 = vless://uuid@host:port?...#name
```

```bash
venv/bin/python3 scripts/generate_mihomo_config.py
docker compose restart mihomo
```

Если рядом лежит `all_nodes.txt` (пул нод для ротации), генератор добавляет
его ноды в каждую группу `pg-account_N` — нужно для `rotation.py`.
Слоты без ссылки в `.env` получат предупреждение и останутся без прокси.

### 3. Запуск скрапера

```bash
docker compose up -d --build           # Linux
docker compose logs -f scraper
# Mac/Windows: docker compose -f docker-compose.bridge.yml up -d --build
```

Без Docker: `pip install -r requirements.txt`, `mihomo -f mihomo_config.yaml`,
`python3 main.py`.

`config.yaml` перечитывается на лету (~5 с). `accounts.yaml` — только при
старте: после правки `docker compose restart scraper`. Содержимое
`cookies/account_N.json` перечитывается на лету.

## Cookies

### Логин-аккаунты (1..10)

1. Первый логин руками (поднимает виртуальный экран и noVNC, ссылку
   открываешь на домашнем ПК):
   ```bash
   venv/bin/python3 scripts/manual_login.py --list
   venv/bin/python3 scripts/manual_login.py account_1
   ```
   Скрипт сам проверит cookies запросом `comments.json` и запишет
   `cookies/account_1.json` + `storage_state/account_1.json`.
2. Дальше `refresh_cookies.py` раз в неделю подтверждает сессию (через тот
   же прокси-порт и тот же движок браузера, что в `impersonate`) и атомарно
   обновляет файл. Разлогиненный аккаунт он **не трогает** — только ERROR и
   ненулевой exit code.
   ```bash
   venv/bin/python3 scripts/refresh_cookies.py --account account_1 [--no-headless] [--dry-run]
   ```

### Гостевые слоты (11..25)

С мая 2026 Reddit отдаёт 403 на `.json` без cookies, но хватает анонимной
сессии из настоящего браузера. `refresh_guest_cookies.py` для каждого слота:
открывает reddit.com через прокси слота (`domcontentloaded` + пауза, а не
`networkidle`), снимает cookies, **верифицирует** их запросом `comments.json`
тем же `impersonate` и только при 200 перезаписывает файл. Слоты, не
прошедшие верификацию, повторяются в конце прогона.

```bash
venv/bin/python3 scripts/refresh_guest_cookies.py                       # все гостевые
venv/bin/python3 scripts/refresh_guest_cookies.py --account account_12
venv/bin/python3 scripts/refresh_guest_cookies.py --no-headless --dry-run
```

Самая короткая из cookies (`session_tracker`) живёт ~2 ч, поэтому таймер — раз
в час.

Альтернатива без браузера: `scripts/test_curl_guest.py --account account_12`
пробует получить гостевые cookies тем же curl_cffi (`--write` — записать). Если
для твоих нод это даёт 200, Playwright для гостей можно не использовать.

### Что происходит, когда cookies протухли

После `max_consecutive_auth_errors` подряд 401/403 воркер **не завершается**
(раньше завершался навсегда и до ручного restart не возвращался): он уходит в
паузу, раз в `auth_dead_cooldown_seconds` пробует запрос, а когда refresh-джоб
кладёт новый файл — подхватывает его, сбрасывает счётчик и продолжает.

### systemd

```bash
sudo cp scripts/systemd/refresh-* /etc/systemd/system/
# поправь User=, WorkingDirectory=, путь к venv в обоих .service
sudo systemctl daemon-reload
sudo systemctl enable --now refresh-cookies.timer refresh-guest-cookies.timer
```

Оба джоба поднимают браузер, поэтому в `.service` стоит общий `flock`
(`/tmp/reddit_scraper_browser.lock`) — одновременно они не запустятся. Гостевой
юнит использует `xvfb-run` (под systemd нет DISPLAY; `apt install xvfb`) —
для headless-режима убери `xvfb-run` и `--no-headless`.

## Смена отпечатка

```bash
venv/bin/python3 scripts/rotate_fingerprints.py --dry-run
venv/bin/python3 scripts/rotate_fingerprints.py --account account_15
venv/bin/python3 scripts/rotate_fingerprints.py --refresh   # + перегенерить cookies
docker compose restart scraper
```

Правит `accounts.yaml` и `refresh_guest_cookies.yaml` синхронно (бэкапы
`*.bak-<время>`). Логин-аккаунты не трогает без `--include-login`.

`scripts/rotation.py` умеет менять ноду на лету через API mihomo, но
оркестрирующего джоба (`refresh_guest_cookies_auto.py`) в репозитории нет —
без него это библиотека, самостоятельно ничего не запускающая.

## Ключевые настройки `config.yaml`

- `stream_subs` — поток (`["all"]`).
- `max_age_seconds` — комментарии старше отбрасываются до дедупа.
- `stream_start_interval_seconds`, `stream_min/max_interval_seconds`,
  `overlap_target_low/high` — слоты планировщика и их самоподстройка по доле
  дублей на первой странице.
- `pagination_max_pages` — `0` = без лимита (фактически ≤10 страниц; останов по
  пересечению с виденными / `max_age`).
- `queue_control.*` — авто-скорость отправки по очереди пайплайна
  (`GET /queue`); `target_rate_per_second` — статичный fallback.
  `QUEUE_CONTROL_URL` (env) перекрывает `url` (нужно в bridge-режиме).
- `batch_max_items`, `token_wait_timeout_seconds`, `seen_cache_size`.
- `ratelimit_safety_margin`, `ratelimit_max_interval_seconds` — личный бюджет
  аккаунта по `X-Ratelimit-*`.
- `base_backoff_seconds`, `max_backoff_seconds`, `max_consecutive_auth_errors`,
  `auth_dead_cooldown_seconds`.
- `cookie_reload_check_interval_seconds`.

## Логи

```
[account_1] r/all стр=2 получено=140 дубли_стр1=38/100 отправлено=61 не_подтв=0 срезано_лимитом=0 догнали=True cooldown=3.2s
[stream] уникальных=42.0/с опросов=1.30/с интервал_слота=0.77s доля_дублей(ewma)=0.38 аккаунтов_готово=8/10
[governor] bpipe_queue=310/2000 upipe=0/200 degraded=False | потолок 80 -> 100/с (реально 72/с) | ...
[health] http_executor: active=0 submitted=5120 completed=5120 ... swaps=0 pool_size=32
```

## Диагностика

```bash
venv/bin/python3 scripts/check_nodes.py                 # все ноды
venv/bin/python3 scripts/check_nodes.py --account account_5
```

Показывает, на каком слое ломается цепочка: интернет хоста → порты mihomo →
VLESS-сервер → нода → Reddit.

## Известные ограничения

- Многоаккаунтовый скрейпинг с датацентровых IP в принципе заметен антиботам;
  регулярные `backoff` в логах — сигнал снижать нагрузку, а не игнорировать.
- Дедуп-кэш только в памяти и обнуляется при рестарте.
- Если `store_items` возвращает `не_подтв > 0` — смотри warning-строки
  `pipeline.py` (недоступен пайплайн или слишком большой `batch_max_items`/413).
- Гостевые профили с Safari/iOS запускаются в Playwright WebKit на Linux:
  TLS/JS-отпечаток браузера при харвесте не идентичен реальному Safari — для
  самих запросов важен только `impersonate`, но если Reddit отвергает такие
  cookies (верификация ≠ 200) — переведи слот на chrome/firefox-профиль.
- `docker-compose.yml` тянет `metacubex/mihomo:latest`; для воспроизводимых
  сборок зафиксируй тег.
