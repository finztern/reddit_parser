# Схема метрик scraper-а

Версия JSON-контракта: `schema: 1`. Производитель — `scraper/metrics.py`;
потребители — `scripts/dashboard.py` и диагностические инструменты.
Валидация — `validate_snapshot()`, тесты — `tests/test_metrics.py`.

Это runtime contract, а не обещание производительности/доступности. Метрики
локальны для одного процесса и сбрасываются после рестарта scraper-а.

## Транспорт

| Канал | Адрес/путь | Поведение |
|---|---|---|
| HTTP | `GET http://127.0.0.1:9100/metrics.json` | Возвращает последний готовый JSON; до первого снимка — HTTP 503 с `{"schema":1,"error":"no_data_yet"}` |
| Snapshot | `state/metrics_snapshot.json` | Атомарная запись; интервал задаётся `metrics.snapshot_interval_seconds` (default 2 с) |
| Event journal | `state/events.jsonl` | JSON Lines, batch flush и ротация по `metrics.events_max_bytes`/`events_backups` |

Bind/port настраиваются секцией `metrics` и переменными `METRICS_BIND`,
`METRICS_PORT`. В bridge compose HTTP port публикуется только на
`127.0.0.1` хоста. `metrics.enabled: false` выключает сбор/endpoint.

Для файлового канала `read_snapshot(path)` возвращает `None`, если файл
отсутствует, JSON повреждён или версия схемы не поддерживается. Это не
гарантирует полную валидацию структуры: для проверки используйте
`validate_snapshot(snapshot)`.

Совместимое добавление необязательных полей не требует новой версии. Удаление
обязательных полей или изменение их значения/семантики требует обновить схему,
валидатор, потребителей и тесты.

## Верхний уровень

| Поле | Тип | Смысл |
|---|---|---|
| `schema` | integer | Версия контракта, `1` |
| `ts` | number | Время снимка, Unix seconds |
| `started_at` | number | Время запуска процесса |
| `uptime_s` | number | Аптайм текущего процесса |
| `global` | object | Сводные окна, очередь, состояния и предупреждения |
| `series` | object | Глобальные кольцевые ряды |
| `accounts` | array | Одна строка на настроенный/наблюдавшийся аккаунт |

## `global`

Объект содержит ключи `windows`, `queue`, `accounts`, `warning` и
`metrics_errors`.

### `global.windows`

Содержит `10s`, `1m`, `15m` и `total`. Первые три — агрегаты последних окон;
`total` — накопление с момента старта процесса. В первые секунды `span_s`
меньше полного окна.

Поля каждого окна:

| Поле | Тип/единица | Смысл |
|---|---|---|
| `span_s` | number, seconds | Фактический интервал нормализации rate |
| `unique_comments_per_sec` | number | Уникальные элементы, добавленные в sender buffer, в секунду |
| `comments_fetched_per_sec` | number | Полученные комментарии (до дедупликации), в секунду |
| `dedupe_ratio` | number или null | `1 - unique/fetched`; null, если fetched = 0 |
| `sent_to_collector_per_sec` | number | Подтверждённые элементы в секунду |
| `send_errors` | integer, items | Не подтверждённые элементы за окно |
| `requests_per_sec` | number | HTTP requests к Reddit в секунду; страница pagination считается запросом |
| `codes` | object of counts | Ключи `200`, `403`, `429`, `5xx`, `other` |
| `empty_listings` | integer | Пустые listings |
| `network_errors` | integer | Ошибки соединения/таймаута |

Классификация кодов строится по результату опроса аккаунта; это не raw access
log каждого HTTP-запроса. Для точной диагностики используйте логи scraper-а.

### `global.queue`

Последнее наблюдаемое состояние коллектора и sender-а:

| Поле | Смысл |
|---|---|
| `size` | `bpipe_queue` либо null до ответа `/queue` |
| `max` | Максимум очереди, если его сообщает коллектор |
| `inflight` | Число элементов в обработке, если оно доступно |
| `degraded` | Флаг деградации коллектора либо null |
| `action` | Действие, сообщённое queue endpoint |
| `buffer` | Текущий объём sender buffer |

Часть полей может быть null, если queue endpoint отключён или ещё не ответил.

### `global.accounts`

Целые счётчики `total`, `active`, `cooldown`, `degraded`, `auth_failed`,
`blocked_node`, `disabled`, `waiting_login`, `needs_login`. `total` совпадает
с количеством строк в верхнеуровневом массиве `accounts`. `starting` — это
состояние отдельной строки аккаунта, отдельного счётчика в `global.accounts`
для него нет.

`warning` содержит строковое предупреждение планировщика или null.
`metrics_errors` — количество внутренних ошибок сборщика.

## `series`

Глобальные ряды содержат 180 корзин по 5 секунд (15 минут), от старой к новой.
Пропущенные корзины заполнены нулями; последняя корзина может быть неполной.

| Поле | Тип значения точки |
|---|---|
| `step_s` | `5` |
| `points` | `180` |
| `ts_end` | Unix seconds |
| `unique_comments` | Уникальные элементы в секунду за корзину |
| `sent_to_collector` | Подтверждённые элементы в секунду |
| `requests` | Reddit requests в секунду |
| `ok200_ratio` | Доля ответов 200; null при нуле запросов |
| `empty_listings` | Число пустых listings за корзину |
| `errors` | Число не-200 requests за корзину |

Эти ряды предназначены для краткосрочного dashboard, не для долговременной
аналитики.

## `accounts[]`

Обязательные поля одной записи:

| Поле | Смысл |
|---|---|
| `name` | Имя аккаунта, обычно `account_N`; уникально в массиве |
| `state` | `active`, `cooldown`, `degraded`, `auth_failed`, `blocked_node`, `disabled`, `waiting_login` или `starting` |
| `since` | Unix seconds начала текущего состояния или null |
| `node` | Из активного slot state (`state/slots/`) либо null |
| `country`, `exit_ip` | Доступные сведения из `state/profiles/` либо null |
| `requests_1h` | Количество запросов за последний час |
| `ok_ratio_1h` | Доля успешных запросов за последний час или null без запросов |
| `last_error` | Последняя метка ошибки или null |
| `last_error_at`, `last_ok_at` | Unix seconds или null |
| `unique_comments_1h` | Уникальные комментарии за последний час |
| `cookie_age_hours` | Возраст cookies по mtime либо null |
| `needs_login` | Boolean |
| `needs_login_reason` | Причина (`first_login`, `no_cookies`, auth label и др.) или null |
| `series` | Per-account короткие ряды |

Per-account `series.step_s` равен 30 с; каждое поле `requests`, `ok`,
`unique` содержит 30 значений, то есть примерно последние 15 минут. Для
`requests_1h` и `unique_comments_1h` сборщик хранит 120 корзин по 30 секунд.

## `state/events.jsonl`

Одна JSON-запись на строку. Основные поля: `ts`, `account` (может быть null),
`event`, `detail`, `level`. Типичные события: `state_change`, `auth_failed`,
`node_blocked`, `relogin_ok`, `node_replaced`; список зависит от вызывающих
модулей.

Журнал ограничен по размеру и числу резервных файлов. Это диагностический
журнал, не аудит-трейл: он локален, ограниченно хранится и не предназначен
для доказательства полного набора действий.

## Изменение схемы

При изменении контракта обновите вместе:

1. `SCHEMA_VERSION`, producer и `validate_snapshot()` в `scraper/metrics.py`;
2. consumer в `scripts/dashboard.py`;
3. тесты `tests/test_metrics.py` и dashboard tests;
4. этот документ.

Горячий путь записи счётчиков не выполняет файловый I/O. Снимок сериализуется
по таймеру, file write выполняется через `asyncio.to_thread`; ошибки метрик
логируются и не должны останавливать scraper.
