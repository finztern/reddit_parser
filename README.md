# Reddit fresh-comments scraper

Опрашивает общий поток комментариев (`www.reddit.com/r/all/comments.json`,
см. `stream_subs`) с нескольких аккаунтов, каждый — через свой VPN-выход
(своя нода VLESS через mihomo), и льёт свежие комментарии в локальный
пайплайн (`POST /store_items`, батчами). Скорость отправки сама
подстраивается под очередь пайплайна.

Внутреннее устройство — в [ARCHITECTURE.md](./ARCHITECTURE.md).
Ротация идентичностей гостевых слотов — раздел ниже и
[ROTATION_SETUP.md](./ROTATION_SETUP.md).

## Аккаунты: 10 логин + 15 гостевых

| Слоты | Тип | Откуда cookies | Кто обновляет |
|---|---|---|---|
| `account_1..10` | **логин** (реальные аккаунты Reddit) | первый логин руками (`manual_login.py`) | `scripts/refresh_cookies.py`, раз в неделю |
| `account_11..25` | **гостевые** (анонимная сессия, без логина) | харвест браузером | `scripts/refresh_guest_cookies.py`, раз в час; смена идентичности — `scripts/rotation_daemon.py` |

Правила, которые нельзя нарушать (джобы пишут в один и тот же
`cookies/account_N.json`):

- `refresh_cookies.py` работает только с `login_accounts` из
  `scripts/refresh_cookies.yaml` (1..10);
- `refresh_guest_cookies.py` работает только со слотами из `guest_accounts`
  в `scripts/refresh_guest_cookies.yaml` (11..25);
- **источник правды для `impersonate`/`user_agent` — `accounts.yaml`**, кроме
  гостевых слотов после ротации (их активный профиль — `state/slots/`).
  Менять отпечатки вручную — через `scripts/rotate_fingerprints.py`.

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
│   ├── state.py                # SeenCache, BackoffState
│   ├── scheduler.py            # PollScheduler: слоты опроса по спросу, самый давний аккаунт первым
│   ├── sender.py               # Sender: буфер + отправка батчей по очереди bpipe, свежие первыми
│   ├── health.py               # мониторинг/своп http_executor
│   ├── identity.py             # state/: заявки на ротацию, активные профили слотов
│   └── worker.py               # account_worker, supervised()
├── scripts/
│   ├── refresh_cookies.py / .yaml          # логин-аккаунты 1..10
│   ├── refresh_guest_cookies.py / .yaml    # гостевые слоты 11..25
│   ├── rotation_daemon.py      # демон ротации идентичностей (хост)
│   ├── manual_login.py         # первый ручной логин (Xvfb + noVNC)
│   ├── generate_mihomo_config.py           # mihomo_config.yaml из vless_accounts.env
│   ├── rotate_fingerprints.py  # смена impersonate+UA (правит accounts.yaml + guest yaml)
│   ├── rotation.py             # библиотека: пул нод, mihomo API, precheck
│   ├── check_nodes.py          # диагностика цепочки mihomo -> нода -> Reddit
│   ├── test_curl_guest.py      # проверка гостевых cookies БЕЗ браузера (curl_cffi)
│   └── systemd/                # timer/service джобов и демона ротации
├── config.yaml                 # тюнинг (hot-reload), в т.ч. секция rotation
├── accounts.yaml               # 25 слотов (читается при старте)
├── vless_accounts.env          # account_N = vless://... (секреты, в .gitignore)
├── all_nodes.txt               # пул нод для ротации, по vless:// на строку (в .gitignore)
├── mihomo_config.yaml          # СГЕНЕРИРОВАН — не редактируй руками
├── cookies/account_N.json      # cookies (Cookie Editor JSON)
├── storage_state/account_N.json# сессии Playwright (логин-аккаунты)
├── state/                      # заявки, активные профили, реестр идентичностей (rw в контейнере)
├── requirements.txt            # зависимости скрапера (в образ)
├── requirements-refresh.txt    # зависимости джобов (playwright, curl_cffi)
├── Dockerfile
├── docker-compose.yml          # Linux (network_mode: host)
└── docker-compose.bridge.yml   # Mac/Windows (ротация в этом режиме не работает)
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

`vless_accounts.env` — по строке на слот:

```
account_11 = vless://uuid@host:port?...#name
```

```bash
venv/bin/python3 scripts/generate_mihomo_config.py
docker compose restart mihomo
```

Если рядом лежит `all_nodes.txt` (пул нод для ротации), генератор добавляет
его ноды в `proxies`, а все ноды — в каждую группу `pg-account_N`.
Слоты без ссылки в `.env` получат предупреждение и останутся без прокси.

### 3. Запуск скрапера

```bash
mkdir -p state && chmod 777 state
docker compose up -d --build           # Linux
docker compose logs -f scraper
# Mac/Windows: docker compose -f docker-compose.bridge.yml up -d --build
```

После любых правок кода в `scraper/` или `main.py` образ нужно
пересобирать (`--build`), иначе контейнер работает со старым кодом.

Без Docker: `pip install -r requirements.txt`, `mihomo -f mihomo_config.yaml`,
`python3 main.py`.

`config.yaml` перечитывается на лету (~5 с). `accounts.yaml` — только при
старте: после правки `docker compose restart scraper`. Содержимое
`cookies/account_N.json` и `state/slots/account_N.json` — на лету.

## Cookies

### Логин-аккаунты (1..10)

1. Первый логин руками (поднимает виртуальный экран и noVNC, ссылку
   открываешь на домашнем ПК):
   ```bash
   venv/bin/python3 scripts/manual_login.py --list
   venv/bin/python3 scripts/manual_login.py account_1
   ```
2. Дальше `refresh_cookies.py` раз в неделю подтверждает сессию и атомарно
   обновляет файл. Разлогиненный аккаунт он **не трогает** — только ERROR
   и ненулевой exit code.
   ```bash
   venv/bin/python3 scripts/refresh_cookies.py --account account_1 [--no-headless] [--dry-run]
   ```

### Гостевые слоты (11..25)

С мая 2026 Reddit отдаёт 403 на `.json` без cookies, но хватает анонимной
сессии из настоящего браузера. `refresh_guest_cookies.py` открывает
reddit.com через прокси слота, снимает cookies, **верифицирует** их запросом
`comments.json` тем же `impersonate` и только при 200 перезаписывает файл.

```bash
venv/bin/python3 scripts/refresh_guest_cookies.py                       # все гостевые
venv/bin/python3 scripts/refresh_guest_cookies.py --account account_12
venv/bin/python3 scripts/refresh_guest_cookies.py --no-headless --dry-run
```

Самая короткая из cookies (`session_tracker`) живёт ~2 ч, поэтому таймер — раз
в час. Слоты, которые в этот момент в ротации, джоб пропускает.

### Что происходит, когда cookies протухли

После `max_consecutive_auth_errors` подряд 401/403 воркер **не завершается**:
он уходит в паузу, раз в `auth_dead_cooldown_seconds` пробует запрос, а когда
refresh-джоб кладёт новый файл — подхватывает его и продолжает.

### systemd

```bash
sudo cp scripts/systemd/refresh-* scripts/systemd/rotation-daemon.service /etc/systemd/system/
# поправь User=, WorkingDirectory=, путь к venv в .service
sudo systemctl daemon-reload
sudo systemctl enable --now refresh-cookies.timer refresh-guest-cookies.timer rotation-daemon
```

Все браузерные джобы берут общий `flock` (`/tmp/reddit_scraper_browser.lock`) —
одновременно они не запустятся.

## Ротация идентичностей гостевых слотов

Когда у гостевого слота `rotation.empty_streak_trigger` пустых листингов
подряд (мягкий бан IP/сессии), воркер кладёт заявку в `state/requests/`.
`scripts/rotation_daemon.py` (на хосте) меняет **идентичность** слота — ноду
VLESS + отпечаток (impersonate/UA/engine/device) + гео, харвестит новые
cookies, верифицирует их и подкладывает слоту. Забаненная идентичность
запоминается с временем бана и возвращается в работу через
`rotation.ban_cooldown_hours`. Всё состояние — в `state/` на хосте, поэтому
рестарты контейнеров его не теряют.

```bash
venv/bin/python3 scripts/rotation_daemon.py --status   # слоты, идентичности, мёртвые ноды, время жизни до бана
journalctl -u rotation-daemon -f
```

Настройки — секция `rotation:` в `config.yaml`; установка и нюансы —
[ROTATION_SETUP.md](./ROTATION_SETUP.md). Пул нод — `all_nodes.txt`.

## Смена отпечатка вручную

```bash
venv/bin/python3 scripts/rotate_fingerprints.py --dry-run
venv/bin/python3 scripts/rotate_fingerprints.py --account account_15
venv/bin/python3 scripts/rotate_fingerprints.py --refresh   # + перегенерить cookies
docker compose restart scraper
```

Правит `accounts.yaml` и `refresh_guest_cookies.yaml` синхронно (бэкапы
`*.bak-<время>`). Логин-аккаунты не трогает без `--include-login`.

## Ключевые настройки `config.yaml`

- `stream_subs` — поток (`["all"]`).
- `max_age_seconds` — комментарии старше отбрасываются до дедупа.
- `stream_start_interval_seconds`, `stream_min/max_interval_seconds`,
  `overlap_target_low/high` — слоты планировщика и их самоподстройка.
- `pagination_max_pages` — `0` = без лимита (фактически ≤10 страниц).
- `queue_control.*` — отправка по очереди пайплайна (`GET /queue`):
  `inflight_target` (128) — сколько должно быть в работе у реплик,
  `queue_target` — сколько держать в очереди ожидания, `send_batch_size`,
  `low_queue_fraction`/`flush_age_fraction` — когда можно слать неполный
  батч, `buffer_target_factor`/`buffer_max_factor` — размер буфера.
  `target_rate_per_second` — fallback, если `/queue` недоступна.
- `batch_max_items`, `seen_cache_size`.
- `ratelimit_safety_margin`, `ratelimit_max_interval_seconds`.
- `base_backoff_seconds`, `max_backoff_seconds`, `max_consecutive_auth_errors`,
  `auth_dead_cooldown_seconds`.
- `cookie_reload_check_interval_seconds`.
- `rotation.*` — ротация идентичностей (см. выше).

## Логи

```
[account_1] r/all стр=2 получено=140 дубли_стр1=38/100 в_буфер=61 дубли=79 буфер=140 догнали=True cooldown=3.2s
[stream] в_буфер=42.0/с опросов=1.30/с интервал_слота=0.77s доля_дублей(ewma)=0.38 аккаунтов_готово=8/10
[send] отправлено=44.8/с батчей=9 (неполных 0) приток=52.0/с буфер=131 | bpipe: очередь=190 inflight=128/128 | протухло=3 вытеснено=0 не_подтв=0 тиков_без_данных=0/100 режим=по очереди
[health] http_executor: active=0 submitted=5120 completed=5120 ... swaps=0 pool_size=32
[account_14] пусто 6 раз подряд — заявка на смену идентичности (state/requests), опрос на паузе
```

## Диагностика

```bash
venv/bin/python3 scripts/check_nodes.py                 # все ноды
venv/bin/python3 scripts/check_nodes.py --account account_5
```

## Известные ограничения

- Многоаккаунтовый скрейпинг с датацентровых IP в принципе заметен антиботам;
  регулярные `backoff` в логах — сигнал снижать нагрузку, а не игнорировать.
- Дедуп-кэш только в памяти и обнуляется при рестарте.
- Если `store_items` возвращает `не_подтв > 0` — смотри warning-строки
  `pipeline.py`.
- Гостевые профили с Safari/iOS запускаются в Playwright WebKit на Linux:
  если Reddit отвергает такие cookies (верификация ≠ 200) — переведи слот на
  chrome/firefox-профиль.
- Версия `curl_cffi` в контейнере не должна быть старее, чем в venv на хосте:
  иначе воркер не сможет применить новый `impersonate`.
- `docker-compose.yml` тянет `metacubex/mihomo:latest`; для воспроизводимых
  сборок зафиксируй тег.
