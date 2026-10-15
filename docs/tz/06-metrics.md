# Модуль 06 — метрики скрапера (контракт данных для панели)

## Цель
Скрапер пишет метрики в локальное хранилище, панель (модуль 07) только читает. Панель не обращается к Reddit и не влияет на скрапер.

## Транспорт
Приоритет: HTTP `GET /metrics.json` на `127.0.0.1:9100` внутри контейнера (порт пробрасывается на localhost) +
резервный файл `state/metrics_snapshot.json` (атомарная запись раз в 2 с). Формат — JSON, схема версионируется (`schema: 1`).

## Содержимое
Глобально (скользящие окна 10 с / 1 мин / 15 мин + суммарно):
- `unique_comments_per_sec`, `comments_fetched_per_sec` (с дублями), `dedupe_ratio`;
- `sent_to_collector_per_sec`, `send_errors`, `queue {size, max, degraded, action}`;
- `requests_per_sec`, коды ответов (200/403/429/5xx), `empty_listings`, `network_errors`;
- `accounts_total / active / cooldown / degraded / auth_failed / blocked_node / disabled`.

По аккаунту:
- `name`, `state`, `since`, `node`, `country`, `exit_ip`, `requests_1h`, `ok_ratio_1h`, `last_error`, `last_error_at`,
  `last_ok_at`, `unique_comments_1h`, `cookie_age_hours`, `needs_login` (bool) + `needs_login_reason`.

Ряды для графиков: кольцевые буферы по 15 минут с шагом 5 с (по глобальным метрикам и по каждому аккаунту — сжато).

## Событийный журнал
`state/events.jsonl` (ротация по размеру): `ts, account, event (state_change, auth_failed, node_blocked, relogin_ok …), detail`.
Панель показывает последние события.

## Требования
- Накладные расходы < 1 % CPU; запись метрик не блокирует цикл опроса.
- Совместимость назад: при отсутствии метрик панель показывает «нет данных», а не падает.

## Приёмка
- Юнит-тесты агрегаторов (окна, скорость, пересчёт состояний) на синтетических событиях.
- Схема описана в `docs/metrics-schema.md`; тест проверяет, что снимок валиден.
