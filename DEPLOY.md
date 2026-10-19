# Развёртывание и эксплуатация

Практическое руководство по Linux-серверу и локальному запуску. Перед
изменением конфигурации сверяйте этот документ с текущими YAML и CLI.
Архитектура приложения — в [ARCHITECTURE.md](./ARCHITECTURE.md); порядок
миграции/ноды — в [ROTATION_SETUP.md](./ROTATION_SETUP.md).

## 1. Поддерживаемая топология

Рекомендуемая production-топология — Linux-хост:

- Docker Compose запускает mihomo и основной Python scraper.
- Host venv запускает Playwright jobs, `login_hub`, фильтр нод, ротационный
  демон, dashboard, backup/restore.
- Scraper общается с mihomo и коллектором на host loopback; в Compose
  используется `network_mode: host`.
- Для передачи состояния контейнер и host tools используют общие каталоги
  проекта `cookies/`, `storage_state/` и `state/`.

`docker-compose.bridge.yml` предназначен для Docker Desktop. Он переопределяет
сетевые адреса коллектора и метрик, но host networking/доступ к mihomo API и
часть host-side ротации отличаются от Linux. Не считайте bridge-режим
эквивалентной production-конфигурацией без отдельной проверки.

Перед настройкой определите:

| Требование | Проверить |
|---|---|
| Python | Python 3.12 рекомендуется: Dockerfile и CI используют 3.12 |
| Docker | Docker Engine и Compose plugin |
| Коллектор | Доступность batch endpoint (по умолчанию `http://127.0.0.1:9000/store_items`) и `/queue` |
| mihomo | Поддержка используемого Meta delay API, controller API доступен локально |
| Browser jobs | RAM под запланированное число Chromium/Firefox/WebKit процессов |
| Доступ к noVNC | Только доверенный LAN/CIDR и firewall; не публиковать в Internet |

Не полагайтесь на примерные оценки RAM или числа нод как на гарантию; измерьте
их на целевом хосте.

## 2. Установка базового окружения

На Debian/Ubuntu установите Docker по официальной инструкции поставщика, затем
системные зависимости для host jobs (список зависит от используемых браузеров):

```bash
sudo apt update
sudo apt install -y git python3.12-venv python3-pip xvfb util-linux curl
# Установите Docker Engine и Compose plugin по документации Docker.
```

Клонируйте нужную ветку репозитория и создайте отдельное Python-окружение:

```bash
git clone https://github.com/finztern/reddit_parser.git reddit_parser
cd reddit_parser

python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt -r requirements-refresh.txt
venv/bin/playwright install-deps chromium firefox webkit
venv/bin/playwright install chromium firefox webkit
```

Установка Playwright browser-ов на хост нужна для login/refresh/rotation jobs,
но не копирует браузеры в Docker image скрапера. Устанавливайте только реально
используемые engine/channel, если хотите сократить размер среды.

Создайте каталоги runtime и ограничьте доступ:

```bash
mkdir -p cookies storage_state state
chmod 700 cookies storage_state state
```

Если контейнер работает не под тем же UID, что host jobs, заранее продумайте
права общего `state/`. Compose монтирует его на запись; не используйте
`chmod 777` как универсальное исправление. Сохраняйте доступ только нужной
учётной записи/группе.

## 3. Конфиденциальные входные данные

Положите локально, не в Git:

- `proxy_accounts.env` — назначение прокси по слотам;
- `all_proxys.txt` — пул доступных кандидатов, по одному прокси на строку (`user:pass@host:port`);
- cookies, Playwright state и остальные runtime-данные.

Проверьте, что файлы игнорируются Git:

```bash
git check-ignore -v proxy_accounts.env all_proxys.txt cookies state storage_state
git status --short
```

Не вставляйте реальные прокси (логин:пароль@хост), cookies, storage state и VNC-пароли в issue,
коммиты, документацию или вывод CI. Секрет mihomo controller, если он нужен,
задаётся отдельно в локальной конфигурации.

## 4. Сгенерировать конфигурацию mihomo и проверить ноды

Генератор читает `proxy_accounts.env` и `all_proxys.txt`, записывает
`mihomo_config.yaml` (он игнорируется Git). Не изменяйте его вручную:

```bash
venv/bin/python3 scripts/generate_mihomo_config.py
docker compose up -d mihomo
curl --fail --silent http://127.0.0.1:9090/proxies >/dev/null
```

Проверьте пул через работающий mihomo:

```bash
venv/bin/python3 scripts/filter_nodes.py
```

Команда пишет отчёт `state/node_report.json`, но не обновляет accepted-pool.
После просмотра результатов сохраните только проверенные ноды:

```bash
venv/bin/python3 scripts/filter_nodes.py --write
```

Не выполняйте массовую выдачу или замену нод, пока не убедились, что тест
попадает в ожидаемый mihomo controller и ответ Reddit интерпретируется верно.
Если ни одна нода не прошла, `--write` намеренно не затирает прежний файл.

## 5. Выбрать и проверить режим парка

Репозиторий поддерживает login- и guest-слоты. Актуальный источник назначений —
`accounts.yaml` вместе с `scripts/refresh_cookies.yaml` и
`scripts/refresh_guest_cookies.yaml`. Не исходите из номера слота без проверки
списков.

### Login-only миграция

Для перевода guest-слотов в login режим сначала сделайте копию проекта или
snapshot runtime-данных. Сначала команды показывают план:

```bash
venv/bin/python3 scripts/convert_to_login.py --all
```

Сверьте, что план содержит ожидаемые аккаунты, гео, proxy-порты и ни один слот
не будет потерян. Затем миграция с `--apply` сохраняет файлы `*.bak-login-*`:

```bash
venv/bin/python3 scripts/convert_to_login.py --all --apply
```

Новый login-слот создаётся выключенным до первого подтверждённого входа.
Сначала проверьте ноды и количество запаса (`fleet.reserve_nodes`), затем
попросите `add_login_slots.py` составить план:

```bash
venv/bin/python3 scripts/add_login_slots.py
```

Проверив соответствие числа хороших нод, списка аккаунтов, уникальности портов
и резерва, применяйте только явно нужное изменение:

```bash
venv/bin/python3 scripts/add_login_slots.py --apply
```

Опции скриптов могут различаться; перед операцией сверяйте CLI в самом файле
или запускайте `--help`. Не добавляйте слоты/ноды к активному парку, если
конфигурация mihomo и account YAML не будут обновлены согласованно.

### Сохранить guest-слоты

Guest cookies обновляет только `refresh_guest_cookies.py`; настройки и список
сверяйте в `scripts/refresh_guest_cookies.yaml`. Ручная проверка одного слота:

```bash
xvfb-run -a venv/bin/python3 scripts/refresh_guest_cookies.py --account account_12 --no-headless
```

Убедитесь, что этот аккаунт действительно находится в guest-списке и его
cookies проверены до включения/запуска worker-а. `fleet.guest_enabled`
управляет гостевой identity rotation, а не самим фактом наличия
`enabled: true` в `accounts.yaml`.

## 6. Login и ручной вход

Покажите текущий login-статус:

```bash
venv/bin/python3 scripts/manual_login.py --list
venv/bin/python3 scripts/login_hub.py --list-queue
```

Один аккаунт можно обслужить через `manual_login.py`; несколько — через
`login_hub.py`. Задайте фактический LAN IP сервера и допустимую CIDR-сеть в
`config.yaml`/CLI. Не оставляйте noVNC открытым на публичном интерфейсе.
Используйте `MANUAL_LOGIN_VNC_PASSWORD` или `LOGIN_HUB_VNC_PASSWORD` через
защищённое окружение/секрет-хранилище; не сохраняйте пароль в YAML или
коммитах.

```bash
venv/bin/python3 scripts/login_hub.py --accounts account_1 account_2
```

По умолчанию login-hub ограничивает число сессий как настройкой, так и
доступной памятью. Сессия с капчей/подтверждением остаётся за оператором до
таймаута. После успешного входа проверьте `cookies/`, `storage_state/`,
`enabled` и вывод `manual_login.py --list` прежде чем включать аккаунт.

## 7. Запуск скрапера

Linux:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --since=10m scraper
```

Scraper читает конфигурацию из bind mounts. `config.yaml` обновляется на лету;
`accounts.yaml` загружен на старте — после его изменения перезапустите
контейнер:

```bash
docker compose restart scraper
```

`store_endpoint` и `queue_control.url` по умолчанию указывают на
`127.0.0.1:9000` хоста в Linux host-network режиме. Если коллектор слушает
другой интерфейс/порт, задайте правильный endpoint и проверьте доступ до
запуска полного парка.

Bridge mode:

```bash
docker compose -f docker-compose.bridge.yml up -d --build
```

В bridge mode compose уже задаёт `host.docker.internal` для коллектора и
метрики, но проверяйте сетевой путь к mihomo и ограничения rotation отдельно.

## 8. Systemd jobs

`scripts/install_systemd.sh` рендерит шаблоны из `scripts/systemd/`, подставляя
пользователя, группу и абсолютный путь проекта. Сначала можно только
отрисовать файлы:

```bash
scripts/install_systemd.sh --out /tmp/reddit-units
```

Затем после проверки установите их на хост:

```bash
sudo scripts/install_systemd.sh --enable
```

В режиме `--enable` включаются `backup-sessions.timer`,
`filter-nodes.timer`, `rotation-daemon.service` и login refresh timer; guest
refresh units ставятся/включаются только с `--with-guest`. Если guest mode
выключен, установщик отключает ранее установленный
`refresh-guest-cookies.timer`. `--no-login` не устанавливает login refresh
units. Сверяйте список с фактически используемыми типами аккаунтов.

Проверка после установки:

```bash
systemctl list-timers --all
systemctl status backup-sessions.timer filter-nodes.timer rotation-daemon.service
journalctl -u rotation-daemon.service --since today
journalctl -u backup-sessions.service --since today
```

Изменение `fleet.node_check_interval_hours` само по себе не меняет
`filter-nodes.timer`: интервал прописан в systemd unit и требует отдельной
правки шаблона/переустановки.

## 9. Резервирование и восстановление

`backup_sessions.sh` копирует `cookies/`, `storage_state/`,
`state/profiles/`. По умолчанию backup root — каталог `backups` рядом с
каталогом проекта (`${ROOT}/../backups`); переопределить можно переменной
`BACKUP_ROOT`. Каталогам выставляется `0700`, файлам `0600`. Скрипт удаляет
только датированные `YYYYMMDD` каталоги старше 14 дней.

Создайте backup перед изменением профиля, аккаунта или ноды:

```bash
scripts/backup_sessions.sh
```

Восстановление из копии:

```bash
scripts/restore_sessions.sh YYYYMMDD
scripts/restore_sessions.sh YYYYMMDD account_12
```

Первый вариант копирует всё содержимое поддерживаемых backup-подкаталогов;
второй копирует только файлы данного аккаунта. Он не удаляет новые файлы и
не заменяет конфигурацию `accounts.yaml`/`config.yaml`. Проверьте результат и
владельца файлов перед запуском jobs. Резерв на том же диске не защищает от
потери хоста; для production используйте отдельный защищённый носитель согласно
политике хранения секретов.

## 10. Метрики, status и диагностика

```bash
venv/bin/python3 scripts/dashboard.py
venv/bin/python3 scripts/dashboard.py --once
venv/bin/python3 scripts/dashboard.py --json
venv/bin/python3 scripts/rotation_daemon.py --status
venv/bin/python3 scripts/filter_nodes.py
curl --fail --silent http://127.0.0.1:9100/metrics.json
```

Dashboard читает metrics endpoint (по умолчанию локальный порт 9100), резервный
`state/metrics_snapshot.json` и `state/events.jsonl`. Формат — в
[docs/metrics-schema.md](./docs/metrics-schema.md). Значение `needs_login` и
состояние парка требуют сопоставления с конфигурацией аккаунтов; сам dashboard
не исправляет cookies и не заменяет ручную проверку.

| Симптом | Что проверить |
|---|---|
| Scraper не стартует | `docker compose logs scraper`, YAML, хотя бы один `enabled: true`, существование cookies и уникальность портов |
| Proxy connection refused | Запущен ли mihomo, сгенерирован ли config, совпадает ли proxy host/port |
| Нода отсутствует в controller | Обновить `mihomo_config.yaml` генератором, затем перезапустить mihomo |
| Все ноды не проходят probe | mihomo API/Meta version, endpoint проверки, connectivity к Reddit; не перезаписывать accepted-pool пустым результатом |
| `auth_failed` | Проверить login статус, актуальность cookies/storage state; выполнить ручной login при необходимости |
| `blocked_node`/`needs_node` | Проверить пул, `all_proxys.reddit_ok.txt`, запас и `rotation_daemon --status` |
| Нет отправки | Проверить collector batch endpoint, `/queue`, `STORE_ENDPOINT`, compose networking |
| Dashboard «нет связи» | Проверить scraper, `metrics.enabled`, bind address и локальный port mapping |
| Backup timer не срабатывает | `systemctl status`, `journalctl -u backup-sessions.service`, права на backup root |

## 11. Проверки перед production и rollback

Перед включением полного парка:

1. Проверить конфиги/порты без сетевого запроса: `python -m pytest -q`.
2. Убедиться, что CI завершился успешно для коммита.
3. На целевом хосте проверить mihomo API и небольшой набор нод.
4. Сделать backup и испытать restore на тестовой копии/аккаунте.
5. Запустить небольшой ограниченный набор, наблюдать логи, очередь, ответы и
   память; расширять только после проверки метрик.

CI не проверяет реальные Reddit/прокси/collector ответы и не является
production smoke test. Для отката конфигурационных миграций используйте
сгенерированные `*.bak-*` файлы или отдельный проверенный Git commit.
Откат systemd units требует восстановить предыдущие templates/units и выполнить
`daemon-reload`; не рассчитывайте на автоматический rollback installer-а.
