# Схема метрик скрапера (`schema: 1`)

Контракт между скрапером (пишет, `scraper/metrics.py`) и панелью (модуль 07, только читает).
Проверка снимка — `scraper.metrics.validate_snapshot()`, тест — `tests/test_metrics.py`.

## Транспорт

| Что | Где | Примечание |
|---|---|---|
| HTTP | `GET http://127.0.0.1:9100/metrics.json` | основной; `503 {"error":"no_data_yet"}` до первого снимка. `METRICS_BIND` / `METRICS_PORT` или секция `metrics:` в `config.yaml`. В bridge-режиме порт публикуется только на `127.0.0.1` хоста |
| Файл | `state/metrics_snapshot.json` | резерв, атомарная запись раз в 2 с (`os.replace`) |
| Журнал | `state/events.jsonl` | по строке JSON на событие, ротация по размеру (`events_max_bytes`, `events_backups`) |

Нет ответа / нет файла / битый JSON / другая `schema` — панель показывает «нет данных»
(`scraper.metrics.read_snapshot()` в этих случаях возвращает `None`, а не исключение).
Добавление полей обратно совместимо; смена смысла или удаление поля = `schema: 2`.

## Верхний уровень

| Поле | Тип | Смысл |
|---|---|---|
| `schema` | int | версия схемы, сейчас `1` |
| `ts` | float | время снимка, unix-секунды |
| `started_at` | float | старт процесса скрапера, unix-секунды |
| `uptime_s` | float | аптайм, секунды |
| `global` | object | глобальные метрики |
| `series` | object | ряды для графиков (15 минут, шаг 5 с) |
| `accounts` | array | метрики по аккаунтам |

## `global`

| Поле | Смысл |
|---|---|
| `windows` | окна `10s`, `1m`, `15m` (скользящие) и `total` (со старта процесса) — поля ниже |
| `queue` | очередь пайплайна (последний ответ `/queue`) |
| `accounts` | счётчики аккаунтов по состояниям |
| `warning` | предупреждение планировщика (`not_enough_accounts`) или `null` |
| `metrics_errors` | сколько раз сам сбор метрик падал (в норме 0) |

### Поля окна (`global.windows.<окно>`)

| Поле | Смысл |
|---|---|
| `span_s` | сколько секунд реально покрывает окно (в первые минуты после старта меньше номинала) |
| `unique_comments_per_sec` | уникальные комментарии, ушедшие в буфер отправки, в секунду |
| `comments_fetched_per_sec` | все полученные комментарии (с дублями) в секунду |
| `dedupe_ratio` | `1 − unique / fetched` за окно; `null`, если ничего не получено |
| `sent_to_collector_per_sec` | подтверждённые коллектором элементы в секунду |
| `send_errors` | не подтверждённые коллектором элементы за окно (штуки) |
| `requests_per_sec` | HTTP-запросов к Reddit в секунду (страница пагинации = запрос) |
| `codes` | запросы по кодам: `200`, `403`, `429`, `5xx`, `other` (штуки за окно) |
| `empty_listings` | пустые первые страницы (200 + пустой Listing) |
| `network_errors` | сетевые ошибки / таймауты |

### `global.queue`

| Поле | Смысл |
|---|---|
| `size` | `bpipe_queue` — элементов в очереди пайплайна (`null`, пока `/queue` не опрашивали) |
| `max` | `bpipe_max` |
| `inflight` | `bpipe_inflight` |
| `degraded` | флаг `degraded` от коллектора |
| `action` | рекомендация коллектора (`slow…`/`pause…`/`stop…`/…) |
| `buffer` | элементов в буфере отправки скрапера |

### `global.accounts`

`total`, `active`, `cooldown`, `degraded`, `auth_failed`, `blocked_node`, `disabled` — по состояниям
планировщика (`scraper/fleet_polling.py`); `waiting_login` — нет входа/cookies (воркер не запущен);
`needs_login` — сколько аккаунтов требуют входа. `total` = число строк в `accounts`.

## `series`

Кольцевые буферы 15 мин × шаг 5 с = 180 точек, от старой к новой, последняя — текущая неполная.
Служебные: `step_s`, `points`, `ts_end`.

| Ряд | Значение точки |
|---|---|
| `unique_comments` | уникальных комментариев/с |
| `sent_to_collector` | отправлено в коллектор/с |
| `requests` | запросов к Reddit/с |
| `ok200_ratio` | доля ответов 200 среди запросов шага; `null`, если запросов не было |
| `empty_listings` | пустых листингов за шаг |
| `errors` | запросов не-200 за шаг |

## `accounts[]`

| Поле | Смысл |
|---|---|
| `name` | имя слота (`account_N`) |
| `state` | `active`, `cooldown`, `degraded`, `auth_failed`, `blocked_node`, `disabled` + `waiting_login`, `starting` (воркер ещё не взял слот) |
| `since` | с какого момента (unix-секунды) аккаунт в этом состоянии |
| `node` | нода из `state/slots/<name>.json` |
| `country` | страна выходного IP из профиля устройства (`state/profiles/`) |
| `exit_ip` | выходной IP из профиля |
| `requests_1h` | запросов за час |
| `ok_ratio_1h` | доля успешных запросов за час; `null`, если запросов не было |
| `last_error` | `rate_or_server`, `ratelimit_low`, `empty_listing`, `auth_401`/`auth_403`, `network` |
| `last_error_at` | unix-секунды |
| `last_ok_at` | unix-секунды последнего успешного опроса |
| `unique_comments_1h` | уникальных комментариев за час |
| `cookie_age_hours` | возраст cookies-файла по mtime (его обновляют refresh-джобы); `null`, если файла нет |
| `needs_login` | нужен ручной вход (`auth_failed`, нет cookies, ждёт первого входа) |
| `needs_login_reason` | `auth_401`/`auth_403`, `no_cookies`, `first_login` или `null` |
| `series` | сжатый ряд за 15 мин, шаг 30 с (30 точек): `requests`, `ok`, `unique` |

## Журнал событий `state/events.jsonl`

Строка: `{"ts": 1700000000.5, "account": "account_3", "event": "state_change", "detail": "active -> auth_failed (auth_403)", "level": "error"}`.

| `event` | Когда |
|---|---|
| `state_change` | любой переход состояния аккаунта (кроме первого наблюдения) |
| `auth_failed` | аккаунт перешёл в `auth_failed` (`detail` — причина) |
| `node_blocked` | аккаунт перешёл в `blocked_node` (заявка на замену ноды) |
| `relogin_ok` | воркер применил новые cookies аккаунта, который был в `auth_failed` |
| `node_replaced` | демон выполнил замену ноды/ротацию |

`level`: `info`, `warn`, `error` — для цвета в панели.

## Накладные расходы

Горячий путь (`record_poll`, `record_send`, `event`) — O(1) без ввода-вывода. Снимок собирается и
сериализуется раз в 2 с в цикле событий, запись файла и журнала — в потоке (`asyncio.to_thread`),
HTTP отдаёт готовые байты. Сбой метрик логируется и не влияет на опрос; `metrics.enabled: false`
отключает модуль целиком.
