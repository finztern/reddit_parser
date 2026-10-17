# Архитектура

Этот документ описывает текущую реализацию в `main.py`, `scraper/` и
`scripts/`. Он дополняет [README.md](./README.md) и не заменяет runbook
[DEPLOY.md](./DEPLOY.md).

## 1. Системная схема

```text
config.yaml ── ConfigStore (hot reload) ───────────────┐
accounts.yaml ─ load_accounts (на старте) ─────────────┤
                                                       ▼
 Reddit ── proxy через mihomo ── account_worker × N ── PollScheduler
                                      │                    │
                                      │ fetch/paginate      │ выдаёт слоты
                                      ▼                    ▼
                                  SeenCache ◄──────────────┘
                                      │ try_claim
                                      ▼
                                    Sender ── GET /queue
                                      │       POST /store_items
                                      ├── MetricsCollector ── HTTP /metrics.json
                                      │                     ├─ state/metrics_snapshot.json
                                      │                     └─ state/events.jsonl
                                      ▼
                                   collector

Host jobs ── cookies/, storage_state/, state/
  refresh_cookies.py / refresh_guest_cookies.py
  manual_login.py / login_hub.py / rotation_daemon.py
  filter_nodes.py / dashboard.py / backup_sessions.sh
```

Scraper и host jobs связывает filesystem: `cookies/`, `storage_state/` и
`state/`. В Linux Compose-контейнере `cookies/` read-only, а `state/` read/write.
Host jobs изменяют файлы atomically, когда это поддерживается реализацией;
scraper проверяет обновления cookies и профилей. Подробная схема volume-ов —
в [docker-compose.yml](./docker-compose.yml) и
[docker-compose.bridge.yml](./docker-compose.bridge.yml).

## 2. Процесс `main.py`

На старте приложение:

1. Загружает `config.yaml` через `ConfigStore`, список аккаунтов из
   `accounts.yaml`.
2. Создаёт общие `SeenCache`, `Sender`, `PollScheduler`, пул потоков для
   блокирующего `curl_cffi`, health-monitor и задачи метрик.
3. Запускает `account_worker` для каждого `enabled: true` аккаунта.
4. Обслуживает signal shutdown; падение core task завершает процесс, чтобы
   Docker `restart: unless-stopped` мог поднять его заново.

`account_worker` под supervision перезапускается после исключения с
экспоненциальной задержкой; штатный return не является ошибкой. Процесс
завершается по SIGTERM/SIGINT, падению критической фоновой задачи или когда
завершились все worker-ы.

Основные модули:

| Модуль | Ответственность |
|---|---|
| `scraper/scheduler.py` | Выдаёт запросные слоты по общему спросу Sender; среди доступных аккаунтов предпочитает давно не опрашивавшиеся |
| `scraper/fleet_polling.py` | Состояния аккаунтов, cooldown, восстановительные пробы и runtime-отключение |
| `scraper/worker.py` | Проверка файлов состояния, получение страниц, фильтрация, ошибки и передача результата |
| `scraper/http_client.py` | `curl_cffi` session per account, headers/cookies, timeouts и Reddit pagination |
| `scraper/state.py` | `SeenCache` с `try_claim → confirm/release` и локальное backoff-состояние |
| `scraper/sender.py` | Буфер свежих элементов; чтение очереди и отправка батчей |
| `scraper/pipeline.py` | Формирование payload и вызов `store_endpoint` |
| `scraper/health.py` | Наблюдение за executor; замена пула при зависших потоках |
| `scraper/identity.py` | Пути к `state/`, чтение заявок и профиля активной идентичности слота |
| `scraper/config.py` | `ConfigStore`, чтение аккаунтов/cookies и `CookieFileWatcher` |
| `scraper/metrics.py` | Счётчики, снимок и события для TUI |

## 3. Запросы, дедупликация и отправка

Все включённые аккаунты читают выбранный поток (обычно `r/all`). Запросы
получают индивидуальные proxy-порты mihomo и cookies аккаунта. Планировщик
ограничивает общий темп и учитывает минимум паузы аккаунта из
`fleet_polling.pause_min_seconds`/`pause_max_seconds`; 429, `Retry-After`,
ошибки сервера и близкий к исчерпанию rate-limit переводят слот в cooldown.

Клиент продолжает pagination, пока не встретит пересечение с уже известными
элементами, более старый комментарий, окончание `after` или предел страниц.
`max_age_seconds` ограничивает свежесть перед отправкой.

`SeenCache` общий для процесса:

1. Новый комментарий временно резервируется через `try_claim`.
2. `Sender` буферизует его и выбирает свежие элементы к отправке.
3. При подтверждении коллектора id фиксируется (`confirm`); при ошибке
   освобождается (`release`), чтобы его можно было повторно обработать.

Если включён `queue_control`, Sender опрашивает `/queue` и согласует отправку
с очередью и inflight коллектора. При выключенной/недоступной очереди действует
fallback `target_rate_per_second`. Размер batch дополнительно ограничивается
серверным пределом 1000 элементов.

`SeenCache` хранится только в RAM. Рестарт очищает его; постоянно хранимой
глобальной дедупликации в проекте нет.

## 4. Состояния аккаунта и конфигурация

Аккаунты в `accounts.yaml` должны иметь уникальные `name` и `proxy_port`,
путь `cookie_file`, `impersonate` и `user_agent`. Включаются только записи
`enabled: true`. Списки login/guest refresh jobs хранятся отдельно; не
запускайте оба вида refresh для одного аккаунта.

`fleet_polling` управляет рабочими состояниями:

| Состояние | Поведение |
|---|---|
| `active` | Может получать слоты |
| `cooldown` | Временно исключён после rate/server/ограничений |
| `degraded` | Частые пустые результаты; возвращается после успешных проб |
| `auth_failed` | После порога auth-ошибок редкая recovery-проба до обновления cookies |
| `blocked_node` | Запрос на замену/проверку ноды |
| `disabled` | Отключён конфигом или runtime-файлом |

`state/runtime_disabled.json` позволяет приостановить слот без правки YAML.
`state/fleet_polling.json` — диагностика scheduler-а; это не источник
конфигурации.

`config.yaml` перечитывается раз в `CONFIG_RELOAD_SECONDS` (по умолчанию 5 с).
`accounts.yaml` читается только при запуске. Пути/параметры с поддержкой
окружения перечислены в `scraper/constants.py`: `CONFIG_PATH`,
`ACCOUNTS_PATH`, `PROXY_HOST`, `STORE_ENDPOINT`, `QUEUE_CONTROL_URL`,
`CONFIG_RELOAD_SECONDS`, `STATE_DIR`, `LOG_FILE`, `LOG_MAX_BYTES`,
`LOG_BACKUP_COUNT` и `EXECUTOR_*`. `METRICS_BIND`/`METRICS_PORT` задают
endpoint метрик. Точный набор переменных — source of truth в коде, не этот
перечень.

## 5. Cookies, профили и browser jobs

| Файл/каталог | Кто пишет/читает |
|---|---|
| `cookies/<account>.json` | Host refresh/login tools пишут; scraper читает и отслеживает изменение |
| `storage_state/<account>.json` | Login browser jobs сохраняют состояние Playwright |
| `state/profiles/<account>.json` | Профиль устройства, валидируемый `scraper/profile_gen.py` |
| `state/slots/<account>.json` | Активная identity slot-профиля; применяется worker-ом поверх статической настройки |
| `state/requests/<account>.json` | Заявка/результат взаимодействия worker с daemon |
| `state/human/<account>.json` | Расписание/итог read-only human session |
| `state/login_hub/status.json` | Состояние login-hub; секреты VNC сюда не записываются |
| `state/runtime_disabled.json` | Runtime pause для выбранных аккаунтов |

`refresh_cookies.py` обслуживает только `login_accounts` из
`scripts/refresh_cookies.yaml`. `refresh_guest_cookies.py` обслуживает только
`guest_accounts` из `scripts/refresh_guest_cookies.yaml`. Cookies нельзя
обновлять конкурентно двумя заданиями.

`manual_login.py` выполняет ручной вход одного аккаунта. `login_hub.py`
запускает несколько браузерных входов, ограниченных настройками RAM,
`login_hub.max_parallel`, LAN bind и CIDR. Пароли Reddit не сохраняются
проектом. `human_session.py` применяется при browser-обслуживании сессий;
он предназначен для read-only взаимодействия. Скрейпер сам браузер не
запускает.

Браузерные задания разделяют lock
`/tmp/reddit_scraper_browser.lock`, чтобы не запускать обслуживающие браузеры
параллельно с другими jobs, которые используют тот же lock.

## 6. Ноды и режимы ротации

`generate_mihomo_config.py` строит `mihomo_config.yaml` из VLESS-файлов.
`filter_nodes.py` проверяет пул через mihomo delay API и пишет отчёт
`state/node_report.json`; с `--write` сохраняет прошедшие проверки ноды
в `all_nodes.reddit_ok.txt`.

У `rotation_daemon.py` два режима, управляемые конфигурацией и типом слота:

- **Login node replacement** (`fleet.node_replace_only`): заменить только
  вышедшую из строя ноду из подходящего запаса. Профиль и session сохраняются;
  слот может потребовать повторной проверки входа.
- **Guest identity rotation**: смена ноды/отпечатка и повторный сбор cookies.
  Доступна только при `fleet.guest_enabled: true`.

Подробный runbook — [ROTATION_SETUP.md](./ROTATION_SETUP.md). Демон и scraper
должны видеть один и тот же `state/`; ноды не должны быть одновременно
назначены нескольким аккаунтам.

`fleet.legacy_scheduler` присутствует как поле совместимости, но в текущем
коде не управляет scheduler-ом.

## 7. Метрики и TUI

`MetricsCollector` получает события опроса/отправки, формирует снимок раз в
`metrics.snapshot_interval_seconds` (по умолчанию 2 с) и предоставляет:

- `GET /metrics.json` (по умолчанию `127.0.0.1:9100`);
- `state/metrics_snapshot.json` — резервный атомарный JSON-снимок;
- `state/events.jsonl` — журнал событий с ограниченной ротацией.

Структура схемы `1` описана в [docs/metrics-schema.md](./docs/metrics-schema.md).
Сервис метрик вспомогательный: его недоступность не должна останавливать
основной scraper.

`scripts/dashboard.py` читает эти данные, может показать состояние login-hub,
а явно вызванные действия `l`/`p` открывают вход или меняют runtime-паузу.
Панель не обращается к Reddit.

## 8. Файлы конфигурации и runtime

- `config.yaml` — поведение scraper-а, fleet, metrics и refresh/human settings.
- `accounts.yaml` — аккаунтные идентификаторы, proxy-порты, cookies и отпечатки.
- `scripts/refresh_cookies.yaml` — login account list и настройки login refresh.
- `scripts/refresh_guest_cookies.yaml` — guest account list и настройки guest refresh.
- `vless_accounts.env`, `all_nodes.txt` — локальные операционные данные, не
  коммитятся.
- `mihomo_config.yaml` — производный, генерируемый файл, не коммитится.

Секреты и runtime state намеренно отделены от кода и документации.
