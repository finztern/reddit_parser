# Reddit comments scraper

Асинхронный Python-скрапер читает публичный поток комментариев Reddit (по
умолчанию `r/all`), делает дедупликацию и отправляет свежие записи пакетами
в настроенный локальный коллектор (`/store_items`). Трафик аккаунтов идёт
через отдельные локальные proxy-порты mihomo.

Этот документ — стартовая карта проекта и быстрый справочник. Подробности:
[архитектура](./ARCHITECTURE.md), [развёртывание](./DEPLOY.md),
[работа с нодами и ротацией](./ROTATION_SETUP.md),
[схема метрик](./docs/metrics-schema.md) и [исходные требования модулей](./docs/tz/00-overview.md).

## Сначала прочитайте

| Если вы хотите… | Начните здесь |
|---|---|
| Понять поток данных и ответственность модулей | [ARCHITECTURE.md](./ARCHITECTURE.md) |
| Поднять проект на Linux-сервере | [DEPLOY.md](./DEPLOY.md) |
| Проверить/заменить ноды или изменить тип парка | [ROTATION_SETUP.md](./ROTATION_SETUP.md) |
| Посмотреть JSON метрик для панели | [docs/metrics-schema.md](./docs/metrics-schema.md) |
| Найти потребителей настройки и узнать reload behavior | [docs/configuration-map.md](./docs/configuration-map.md) |
| Изменить протокол обмена через `state/` | [docs/state-protocols.md](./docs/state-protocols.md) |
| Найти реализацию функции | `main.py`, `scraper/`, `scripts/`; исходные ТЗ в `docs/tz/` не являются runbook |
| Быстро сориентировать coding assistant | [CLAUDE.md](./CLAUDE.md) |

## Состав проекта

```text
main.py                     Сборка и lifecycle приложения
scraper/
  config.py                 ConfigStore, чтение аккаунтов и watcher cookies
  worker.py                 Цикл аккаунта, fetch -> filter -> claim -> buffer
  http_client.py            Reddit HTTP/curl_cffi, pagination, результаты запросов
  scheduler.py              Общий темп, выдача слотов, спрос и runtime pause
  fleet_polling.py          Состояния аккаунтов, cooldown и recovery
  sender.py                 Буфер, backpressure, очередь и доставка батчей
  pipeline.py               Payload для collector и подтверждение ответа
  state.py                  In-memory SeenCache и локальный retry backoff
  health.py                 Обнаружение зависших потоков и смена executor
  identity.py               Файловый протокол worker <-> rotation daemon
  profile_gen.py            Fingerprint профили, validation, geo refresh
  geo.py / geoip.py         Локаль и разрешение geo через IP
  metrics.py                Snapshot, event journal и HTTP endpoint
  grouping.py / models.py   Группировка аккаунтов и общие модели
  constants.py / utils.py   Пути, defaults, logging и общие функции
scripts/
  refresh_*                 Обслуживание login/guest cookies
  manual_login.py           Ручной вход одного аккаунта
  login_hub.py              Ограниченный LAN/noVNC вход в несколько аккаунтов
  human_session.py          Read-only browser maintenance
  filter_nodes.py           Проверка пула и отчёт годных нод
  fetch_free_proxies.py     Бесплатные прокси (ProxyScrape): скачать, проверить, добавить в пул
  sync_subscription.py      Суточная синхронизация VLESS-подписки: закреплённые за аккаунтами ноды
  add_*_slots.py            Планирование/добавление guest или login слотов
  convert_to_login.py       План и миграция guest -> login
  reidentify.py             Замена ноды и профиля слота (регистрация не идёт)
  rotation_daemon.py        Обработка node/identity заявок worker-ов
  profile.py                Создание, проверка и миграция fingerprint профилей
  dashboard.py              TUI для метрик и runtime pause/login actions
  backup_sessions.sh        Резервная копия cookies и профилей
  restore_sessions.sh       Восстановление резервной копии
tests/                      Offline unit/integration tests по компонентам
docs/
  configuration-map.md     Настройки, consumers и reload behavior
  metrics-schema.md         Публичный JSON contract для метрик
  state-protocols.md       Протоколы файлов между workers/jobs/dashboard
  tz/                       Исторические ТЗ; реализация и тесты — актуальнее
```

Главные входные данные: `config.yaml`, `accounts.yaml` и два файла настроек
refresh-скриптов. Файл `mihomo_config.yaml` генерируется из секретных VLESS
списков; не редактируйте его вручную.

## Режимы аккаунтов и конфигурация

Роли аккаунтов определяются совместно по `accounts.yaml`,
`scripts/refresh_cookies.yaml` (`login_accounts`) и
`scripts/refresh_guest_cookies.yaml` (`guest_accounts`):

| Тип | Кто обновляет сессию | Особенность |
|---|---|---|
| Login | `refresh_cookies.py`, первый вход через `manual_login.py` или `login_hub.py` | Для login-слота в `accounts.yaml` используется `login: true`; перед включением убедитесь, что вход и cookies уже проверены |
| Guest | `refresh_guest_cookies.py` | Анонимная сессия; гостевая ротация зависит от `fleet.guest_enabled` |

Не добавляйте один слот одновременно в login- и guest-списки: оба задания
обновляют `cookies/<account>.json`. Утилита `convert_to_login.py` сначала
показывает план; только с `--apply` меняет файлы, сохраняя резервные копии.
У нового login-слота `enabled: false` до успешного первого входа.

В текущем checked-in fixture роли для refresh jobs заданы списками; записи
`accounts.yaml` не все содержат явный `login: true`. Миграция
`convert_to_login.py` добавляет marker на переведённые слоты. Поэтому перед
запуском проверьте фактические списки слотов, `enabled`, `login`, ноды и
доступность cookies; особенно важны marker-ы для login-specific статусов,
node-replacement и dashboard. Репозиторий содержит конфигурационные примеры,
а не описание состояния production-хоста. В частности,
`fleet.guest_enabled: false` выключает гостевую ротацию; само по себе оно не
выключает уже включённые записи в `accounts.yaml`.

Основные секции `config.yaml`:

| Секция/ключ | Назначение |
|---|---|
| `stream_subs`, `fetch_limit`, `pagination_*`, `max_age_seconds` | Источник, размер страницы, догоняющая пагинация и окно свежести |
| `queue_control`, `store_endpoint` | Регулирование нагрузки по очереди коллектора и адрес отправки |
| `fleet_polling` | Общий темп, паузы по аккаунтам, состояния, cooldown и ручные runtime-паузы |
| `rotation` | Пул нод, mihomo API, триггеры и лимиты ротации |
| `fleet` | Настройки login-парка, запас нод и переключатели совместимости |
| `login_hub` | LAN bind, CIDR, порты и лимит параллельного входа |
| `human` | Ограниченная read-only сессия обслуживания браузера |
| `metrics` | Локальный endpoint, период снимка и ротация журнала |

`config.yaml` перечитывается примерно раз в 5 секунд. `accounts.yaml`
загружается при запуске контейнера: после изменения перезапустите scraper.
Файлы cookies и JSON-состояние наблюдаются/перечитываются по своим правилам;
см. [ARCHITECTURE.md](./ARCHITECTURE.md).

Для изменения кода пользуйтесь [CLAUDE.md](./CLAUDE.md) как индексом «задача →
модули → тесты». Архитектура описывает lifecycle и владение состоянием;
[DEPLOY.md](./DEPLOY.md) и [ROTATION_SETUP.md](./ROTATION_SETUP.md) — только
операционные процедуры.

`fleet.legacy_scheduler` сейчас только описан в настройках/defaults и не
переключает реализацию планировщика. Не используйте его как рабочий rollback
переключатель без отдельной реализации.

## Локальная разработка и тесты

Используется Python 3.12 в Docker и GitHub Actions. Утилитам на хосте нужен
отдельный venv: браузерные зависимости Playwright не входят в контейнер
скрапера.

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-refresh.txt pytest pytest-cov diff-cover
venv/bin/playwright install chromium firefox webkit

python -m compileall -q main.py scraper scripts tests
python -m pytest -q --cov=main --cov=scraper --cov-report=term-missing
```

Некоторые tests подменяют сетевые клиенты и запускают скрипты в синтетических
копиях проекта; они не проверяют доступность Reddit, VLESS-провайдеров,
mihomo, коллектора или production-хоста.

GitHub Actions workflow [`.github/workflows/ci.yml`](./.github/workflows/ci.yml)
запускает на push и pull request установку зависимостей, `compileall` и полный
pytest-набор. Таблица покрытия добавляется в summary запуска, HTML/XML-отчёт
публикуется как artifact `coverage-report`. Для pull request дополнительно
проверяется покрытие изменённых строк в scope `main.py` и `scraper/` с
начальным порогом 80%; скрипты пока не входят в coverage scope. Жёсткого
минимального порога для всего проекта нет, поэтому непокрытые legacy модули не
блокируют несвязанные изменения. Ruff пока не является CI gate: в существующем
коде есть неисправленные lint-замечания.
`scripts/check_docs.py` также проверяет локальные Markdown-ссылки, пути в
карте `CLAUDE.md` и полноту списка ключей `config.yaml` в
`docs/configuration-map.md`.

## Развёртывание и запуск

Для нового Linux-хоста используйте [DEPLOY.md](./DEPLOY.md), а не
скопированные команды из исходных ТЗ. Кратко:

1. Установить Docker Compose, Python venv, Playwright и системные пакеты.
2. Создать runtime-каталоги; поместить `vless_accounts.env` и `all_nodes.txt`
   вне Git и с ограниченными правами.
3. Сгенерировать конфигурацию mihomo, запустить mihomo и проверить ноды.
4. Проверить план миграции/выдачи нод; выполнять запись только после проверки.
5. Создать и проверить cookies, запустить scraper, затем включать systemd.

Linux-вариант `docker-compose.yml` использует host networking: он рассчитан
на локальные proxy-порты mihomo и коллектор на `127.0.0.1`. Для Docker Desktop
есть `docker-compose.bridge.yml`; проверьте в нём адрес коллектора и помните,
что Linux-only управление mihomo с хоста/контейнера в bridge-режиме
ограничено.

```bash
docker compose up -d --build
docker compose logs -f scraper
```

После изменения `main.py` или `scraper/` пересоберите образ с `--build`.

## Runtime-данные и безопасность

| Путь | Содержимое | Где используется |
|---|---|---|
| `cookies/` | Reddit cookies | Host refresh jobs пишут; контейнер scraper монтирует read-only |
| `storage_state/` | Playwright login state | Login jobs |
| `state/` | Снимки метрик, события, профили, заявки, паузы, отчёты | Общий каталог контейнера и host tools |
| `backups/` | Бэкапы сессий; по умолчанию `../backups` относительно проекта | Вне checkout |
| `vless_accounts.env`, `all_nodes*.txt`, `all_proxys*.txt`, `all_proxies*.txt` | Секретные/операционные данные нод | Host tools и mihomo generator |

Перечисленные файлы и каталоги должны оставаться вне Git. Не публикуйте
содержимое cookies, Playwright `storage_state`, пароли noVNC, VLESS URI или
логи с чувствительными заголовками. См. [.gitignore](./.gitignore) и
[DEPLOY.md — безопасность и резервирование](./DEPLOY.md#9-резервирование-и-восстановление).

Вход noVNC ограничивайте доверенным LAN/CIDR и firewall-правилами. Не
выставляйте порты входа или metrics endpoint в публичный интернет.
`login_hub` и `manual_login` предназначены для ручного входа; пароль Reddit
проект не должен сохранять или печатать.

## Повседневные команды

```bash
# Статус login-слотов и очереди ручного входа
venv/bin/python3 scripts/manual_login.py --list
venv/bin/python3 scripts/login_hub.py --list-queue

# Метрики; --once и --json пригодны для логов/автоматизации
venv/bin/python3 scripts/dashboard.py
venv/bin/python3 scripts/dashboard.py --once
venv/bin/python3 scripts/dashboard.py --json

# Проверка пула и отчёт
venv/bin/python3 scripts/filter_nodes.py
venv/bin/python3 scripts/filter_nodes.py --write
venv/bin/python3 scripts/rotation_daemon.py --status

# Ручной backup и restore (дата YYYYMMDD)
scripts/backup_sessions.sh
scripts/restore_sessions.sh 20261017
scripts/restore_sessions.sh 20261017 account_12

# Проверка и запуск Docker
docker compose ps
docker compose logs --since=10m scraper
```

Точные флаги и порядок операций описаны в [DEPLOY.md](./DEPLOY.md) и
[ROTATION_SETUP.md](./ROTATION_SETUP.md). Не запускайте `--apply`, системные
изменения или миграции на production, не изучив план и не создав проверяемый
backup.

## Ограничения

- Ответы Reddit, доступность IP-нод и rate limits меняются независимо от
  проекта; CI не проверяет их.
- `SeenCache` находится в памяти и очищается при рестарте; доставка
  контролируется ответом коллектора, но это не распределённое постоянное
  хранилище дедупликации.
- Ротация нод требует работающего mihomo API и корректных нод в пуле.
- Поле `fleet.legacy_scheduler` не реализует переключение scheduler.
- ТЗ в `docs/tz/` сохраняют исторические критерии и могут содержать требования,
  которые не являются гарантированными характеристиками текущего кода.
