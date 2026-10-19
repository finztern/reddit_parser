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

### 3.1 Один цикл опроса

Последовательность важна: scheduler владеет eligibility аккаунтов, worker
выполняет один выданный слот, а `Sender` отдельно управляет доставкой.

1. `Sender.demand()` сообщает scheduler-у, нужен ли ещё сбор данных. При
   полном буфере новые Reddit-запросы останавливаются, но sender продолжает
   отправку и очистку буфера.
2. `PollScheduler` выбирает живой и готовый слот, учитывая состояние аккаунта,
   минимальную паузу, общий темп и спрос. Опросы не запускаются независимо по
   таймеру каждого аккаунта.
3. `account_worker` проверяет запросы ротации и обновления профиля/cookies,
   затем получает страницы через `fetch_comments`. Pagination останавливается
   на пересечении с известным ID, по возрасту, по завершении `after` или по
   `pagination_max_pages`.
4. Worker отбрасывает невалидные и старые payload-ы, вызывает
   `SeenCache.try_claim()` и передаёт успешные claims в `Sender.put_many()`.
   Claims в буфере продолжают считаться pending и защищают от дублей между
   параллельными worker-ами.
5. `Sender` выбирает содержимое батча и вызывает `send_batch_to_store()`.
   Только после подтверждения вызывается `SeenCache.confirm()`; неподтверждённые,
   просроченные или вытесненные ID освобождаются через `release()`.
6. Worker сообщает итог опроса через `PollScheduler.release()`. Этот результат
   обновляет состояние/паузы аккаунта и статистику scheduler-а; sender не
   определяет здоровье аккаунта.

### 3.2 Обработка результатов опроса

| Результат Reddit | Состояние/действие | Что важно при изменениях |
|---|---|---|
| Успешный непустой Listing | Формирование payload, claim, буферизация; reset последовательных ошибок | Получение данных не означает, что collector их уже подтвердил |
| Пустой Listing | Нарастающая пауза; после порога состояние может стать `degraded` | Пустой ответ не считать успешным восстановлением |
| 429 или 5xx | Cooldown конкретного аккаунта; остальные слоты продолжают работать | Уважать `Retry-After`; не блокировать весь парк из-за одного аккаунта |
| 401/403 | Auth backoff; после порога `auth_failed` и редкие пробы | Новые cookies перечитываются worker-ом; для login-403 может быть заявка на замену ноды |
| Сетевая ошибка | Ограниченный backoff и уведомление scheduler-а | Не превращать сетевую ошибку в пустой успешный результат |
| Ошибка/неподтверждённый ответ collector-а | Sender освобождает неуспешные claims для повторной обработки | Не подтверждать ID только из-за попытки POST |
| Исключение из worker | `supervised()` перезапускает воркер с экспоненциальной задержкой | `CancelledError` проходит наружу; штатное завершение worker не перезапускается |

Пороговые значения, точные классификаторы ошибок и тайминги задаются в коде и
конфигурации (`scraper/worker.py`, `scraper/fleet_polling.py`,
`scraper/http_client.py`, `config.yaml`). Таблица описывает контракт, а не
полный список status code или все числовые defaults.

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
конфигурации. Протоколы файлов и их writers/readers описаны в
[docs/state-protocols.md](./docs/state-protocols.md); карта потребителей настроек
и правил reload находится в [docs/configuration-map.md](./docs/configuration-map.md).

`config.yaml` перечитывается раз в `CONFIG_RELOAD_SECONDS` (по умолчанию 5 с).
`accounts.yaml` читается только при запуске. Пути/параметры с поддержкой
окружения перечислены в `scraper/constants.py`: `CONFIG_PATH`,
`ACCOUNTS_PATH`, `PROXY_HOST`, `STORE_ENDPOINT`, `QUEUE_CONTROL_URL`,
`CONFIG_RELOAD_SECONDS`, `STATE_DIR`, `LOG_FILE`, `LOG_MAX_BYTES`,
`LOG_BACKUP_COUNT` и `EXECUTOR_*`. `METRICS_BIND`/`METRICS_PORT` задают
endpoint метрик. Точный набор переменных — source of truth в коде, не этот
перечень.

### Владельцы конфигурации и reload

| Данные | Владелец во время работы | Как применяются изменения |
|---|---|---|
| `config.yaml` | `ConfigStore` | YAML перечитывается циклом reload; отдельные параметры используются при следующем обращении к `get()` |
| `accounts.yaml` | `main.py` / `load_accounts()` | Список аккаунтов загружается при старте; изменение требует перезапуска scraper-а |
| Cookies | `CookieFileWatcher` внутри account worker | Проверка по mtime с ограничением частоты stat; валидный изменившийся файл применяется в runtime |
| Активный slot profile | `SlotProfileWatcher` внутри account worker | Изменение `state/slots/<name>.json` применяется при последующей проверке watcher-а |
| Постоянный device profile | `state/profiles/<name>.json` и `profile_gen.py` | Создаётся/меняется профильными утилитами; worker использует его как постоянный fingerprint |
| Runtime pause | `state/runtime_disabled.json` | Scheduler перечитывает runtime-файл; YAML-конфиг при этом не меняется |
| Запрос на ротацию | `state/requests/<name>.json` | Worker пишет заявку, daemon обрабатывает и обновляет статус; worker забирает результат |

Порядок источников fingerprint в worker: постоянный device profile
`state/profiles/<name>.json` важнее slot profile и `accounts.yaml`; slot profile
важнее `accounts.yaml`, если постоянного профиля ещё нет. Slot profile задаёт
активную ноду и может нести текущий fingerprint для legacy/guest-сценария.
Для аккаунта с постоянным device profile смена ноды должна обновлять geo-группу,
но не менять постоянный fingerprint автоматически.

Не вводите второй watcher или независимый кэш для тех же данных, пока не
определено, кто владеет записью и когда изменение должно стать видимым.
Некорректное/пустое обновление cookies не должно затирать последние рабочие
cookies; атомарная замена файла снижает риск чтения частично записанного JSON.

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

`generate_mihomo_config.py` строит `mihomo_config.yaml` из файлов с прокси (`proxy_accounts.env`, `all_proxys.txt`).
`filter_nodes.py` проверяет пул через mihomo delay API и пишет отчёт
`state/node_report.json`; с `--write` сохраняет прошедшие проверки ноды
в `all_proxys.reddit_ok.txt`.

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
- `proxy_accounts.env`, `all_proxys.txt` — локальные операционные данные, не
  коммитятся.
- `mihomo_config.yaml` — производный, генерируемый файл, не коммитится.

Секреты и runtime state намеренно отделены от кода и документации.

## 9. Как выбрать точку изменения

Используйте этот маршрут, чтобы не чинить симптом на неверном уровне:

| Требуется изменить… | Начать с… | Соседние контракты |
|---|---|---|
| Классификацию Reddit-ответа / pagination | `scraper/http_client.py` | `PollResult` и его потребители в `worker.py`; scheduler outcome и тесты HTTP/worker |
| Что считается старым/валидным комментарием | `scraper/worker.py`, `scraper/pipeline.py` | Payload shape в `docs/tz/` (как исторический контекст) и contract tests `test_pipeline_payload.py` |
| Темп, распределение слотов, паузы | `scraper/scheduler.py`, `scraper/fleet_polling.py` | `Sender.demand()`, `PollScheduler.release()`, `tests/test_fleet_polling.py` |
| Retry/дедуп/подтверждение доставки | `scraper/sender.py`, `scraper/state.py`, `scraper/pipeline.py` | `try_claim → confirm/release`; `tests/test_delivery_path.py`, `test_sender_edges.py` |
| Реакцию на новые cookies или node/profile update | `scraper/config.py`, `scraper/identity.py`, `scraper/worker.py` | Пути и владельцы файлов из раздела 5; тесты worker и identity |
| Форму метрик или поведение панели | `scraper/metrics.py` | `docs/metrics-schema.md`, `scripts/dashboard.py`, `tests/test_metrics.py`, `tests/test_dashboard.py` |
| Login/guest refresh или browser flow | Конкретный скрипт `scripts/` | Его YAML-настройки, общий browser lock, `DEPLOY.md`; тест соответствующего сценария |
| Node pool или ротацию | `scripts/filter_nodes.py`, `scripts/rotation_daemon.py` | `ROTATION_SETUP.md`, `state/requests/` и `state/slots/` протокол |

Перед правкой межпроцессного протокола найдите обе стороны: writer и reader.
Перед изменением JSON/API контракта найдите producer, все consumers, schema
documentation и contract tests. `CLAUDE.md` содержит быстрый индекс модулей и
тестов; этот документ описывает взаимодействия, а `DEPLOY.md` и
`ROTATION_SETUP.md` остаются операционными инструкциями.
