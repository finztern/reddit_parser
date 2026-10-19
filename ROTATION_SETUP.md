# Парк аккаунтов, пул нод и замена нод

Этот документ описывает фактические сценарии для `filter_nodes.py`,
`convert_to_login.py`, `add_login_slots.py` и `rotation_daemon.py`.
Общие шаги установки — [DEPLOY.md](./DEPLOY.md); логика worker-ов —
[ARCHITECTURE.md](./ARCHITECTURE.md).

## 1. Источники конфигурации

| Файл | Что задаёт |
|---|---|
| `accounts.yaml` | Слоты, enabled-флаг, proxy-порт, cookies path, login marker, UA/TLS fingerprint |
| `scripts/refresh_cookies.yaml` | Явный список login-аккаунтов и настройки browser refresh |
| `scripts/refresh_guest_cookies.yaml` | Guest-аккаунты и настройки получения анонимных cookies |
| `config.yaml` → `fleet` | Резерв нод, node-replacement политика и guest-enabled flag |
| `config.yaml` → `rotation` | Пул, mihomo API, cooldown, лимиты и параметры daemon |
| `proxy_accounts.env` | Текущая привязка `account_N = http://user:pass@host:port` |
| `all_proxys.txt` | Пул HTTP(S)/SOCKS5-прокси, по одному в строке (`user:pass@host:port`, `http://user:pass@host:port` или `host:port:user:pass`, опционально `#имя`/`#us1`) |
| `all_proxys.reddit_ok.txt` | Пул, который прошёл последнюю проверку `filter_nodes.py --write` |
| `state/` | Отчёты, идентичности, заявки и флаги текущего хоста |

Не добавляйте аккаунт одновременно в login- и guest-refresh списки. Оба job-а
пишут в `cookies/<account>.json`.

## 2. Проверить пул нод

Сначала сгенерируйте и перезапустите mihomo; затем проверьте состояние пула:

```bash
venv/bin/python3 scripts/generate_mihomo_config.py
docker compose up -d mihomo
venv/bin/python3 scripts/filter_nodes.py
```

`filter_nodes.py` использует mihomo delay API для проверки заданного URL и
сохраняет отчёт в `state/node_report.json`. Без `--write` accepted-pool не
изменяется. После просмотра результата можно сохранить прошедшие ноды:

```bash
venv/bin/python3 scripts/filter_nodes.py --write
```

При пустом результате скрипт завершается ошибкой и не перезаписывает прежний
`all_proxys.reddit_ok.txt`. Проверяйте controller/Meta API, URL probe и
доступность сети до любых изменений слотов.

## 3. Перевести guest-слоты в login-режим

`convert_to_login.py` работает по принципу plan → apply. Без `--apply` файлы
не меняются:

```bash
venv/bin/python3 scripts/convert_to_login.py --all
venv/bin/python3 scripts/convert_to_login.py --accounts 11-15,20
```

Проверьте список, порты и гео в плане. С `--apply` скрипт:

- удаляет выбранные слоты из `guest_accounts` и `rotation.slots`;
- добавляет их в login-конфигурацию;
- помечает `login: true`, задаёт `storage_state/<account>.json`;
- отключает новые login-слоты (`enabled: false`) до первого входа;
- создаёт `*.bak-login-*` рядом с изменёнными YAML-файлами.

```bash
venv/bin/python3 scripts/convert_to_login.py --all --apply
```

Это не регистрирует аккаунты и не создаёт login-сессии. Выполните ручной вход
и проверьте аккаунт до включения.

## 4. Добрать login-слоты

Перед добором должны существовать свежий `all_proxys.reddit_ok.txt`,
загруженный в mihomo пул и согласованные исходные настройки. По умолчанию
`add_login_slots.py` читает число резерва из `fleet.reserve_nodes` (обычно 6)
и годные ноды из filtered pool. Команда сначала строит план:

```bash
venv/bin/python3 scripts/add_login_slots.py
```

Посмотрите число слотов, новые имена/порты, назначенные ноды и оставшийся
резерв. Только затем применяйте:

```bash
venv/bin/python3 scripts/add_login_slots.py --apply
venv/bin/python3 scripts/generate_mihomo_config.py
docker compose restart mihomo
```

Полный список флагов зависит от версии команды; используйте
`venv/bin/python3 scripts/add_login_slots.py --help`. Изменение файлов
конфигурации не включает автоматически новые аккаунты и не подтверждает
успешность login.

## 5. Login-only node replacement

Если `fleet.node_replace_only: true`, daemon для login-слота заменяет только
ноду: профиль устройства не пересоздаётся, cookies/storage state не
перехарвестятся. После смены IP слот может быть помечен как требующий проверки
входа. Для кандидатов используется accepted pool, если включён
`fleet.use_reddit_ok_file`.

Диагностика:

```bash
venv/bin/python3 scripts/rotation_daemon.py --status
journalctl -u rotation-daemon.service -f
```

Для каждой замены нужны работающий mihomo controller, общий с контейнером
каталог `state/`, корректная привязка proxy-группы и свободная хорошая нода.
Если резерва нет, daemon не должен занимать ноду другого слота; пополните
filtered pool и повторите диагностику.

## 6. Guest identity rotation (совместимый режим)

Старая guest-ротация отличается от login node replacement: при разрешённом
guest-режиме она может поменять ноду/отпечаток и получить новые cookies.
Она доступна только для аккаунтов из `rotation.slots` и при
`fleet.guest_enabled: true`. Guest refresh timer устанавливается только с
`scripts/install_systemd.sh --with-guest`.

Перед включением проверьте, что:

1. Указаны целевые guest slots в `rotation.slots`.
2. Эти слоты находятся в `guest_accounts`, а не в `login_accounts`.
3. `all_proxys.txt` содержит незанятые кандидаты; mihomo уже загружен сгенерированной конфигурацией.
4. Backup сессий создан и тест восстановления выполнен.

Не включайте `--with-guest` как способ включить login node replacement.
Два механизма имеют разные входные данные и разное влияние на сессию.

## 7. Проверка после изменения

```bash
python -m pytest -q tests/test_repo_config.py tests/test_login_fleet.py tests/test_systemd_install.py
venv/bin/python3 scripts/filter_nodes.py
venv/bin/python3 scripts/rotation_daemon.py --status
```

После конфигурационных изменений обновите mihomo и перезапустите scraper, если
менялся `accounts.yaml`. `config.yaml` подхватывается scraper-ом на лету,
однако host daemon и systemd timer должны получить ту же актуальную версию.

## 8. Откат

До изменения сделайте backup runtime-сессий и сохраните копию текущих YAML.
Скрипты конвертации/добора оставляют собственные `*.bak-*`; проверьте timestamp
и содержимое перед восстановлением. После YAML rollback:

1. Перегенерируйте mihomo config.
2. Перезапустите mihomo и scraper.
3. Проверьте systemd units/timers и статус демона.
4. Проверьте, что guest/login refresh timers соответствуют восстановленному режиму.

`fleet.legacy_scheduler` пока не переключает scheduler и не является rollback
механизмом.

## Ветка `proxy-only`: прокси вместо VLESS

В этой ветке VLESS не поддерживается: каждый аккаунт ходит в Reddit через свой
HTTP(S)/SOCKS5-прокси, mihomo остаётся локальным шлюзом (порт слота -> прокси).

1. Положите в корень проекта `all_proxys.txt` — по одному прокси в строке:
   `user:pass@host:port`, `http://user:pass@host:port` или `host:port:user:pass`
   (необязательный хвост `#us1` задаёт имя и страну). Файл в `.gitignore`/`.dockerignore`.
2. `python3 scripts/generate_mihomo_config.py && docker compose restart mihomo` —
   все прокси пула загружаются в mihomo.
3. `python3 scripts/filter_nodes.py --write` — через каждый прокси проверяется
   `https://www.reddit.com/` (нужен HTTP 200), замеряется задержка. В
   `all_proxys.reddit_ok.txt` годные прокси записываются **от самого быстрого к
   самому медленному**; страна прокси определяется по реальному выходному IP
   (`state/proxy_geo.json`, отключается `--no-geo`) — по ней выбираются
   locale/timezone слота.
4. `python3 scripts/add_login_slots.py` (план) и затем `--apply` — самые быстрые
   свободные годные прокси привязываются к слотам; один прокси = один аккаунт
   (уникальный IP). Остаток (`fleet.reserve_nodes`) остаётся запасом, демон
   ротации заменяет сгоревший прокси следующим по скорости.

`filter_nodes.py` стоит гонять по таймеру: скорость прокси меняется. Привязка
уже выданных слотов при этом не пересматривается — меняется только замена
сгоревших.
