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
| `vless_accounts.env` | Текущая привязка `account_N = vless://...` |
| `all_nodes.txt` | Кандидатный пул VLESS, по одной ссылке на строку |
| `all_nodes.reddit_ok.txt` | Пул, который прошёл последнюю проверку `filter_nodes.py --write` |
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
`all_nodes.reddit_ok.txt`. Проверяйте controller/Meta API, URL probe и
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

Перед добором должны существовать свежий `all_nodes.reddit_ok.txt`,
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
3. `all_nodes.txt` содержит незанятые кандидаты; mihomo уже загружен сгенерированной конфигурацией.
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

## Два источника узлов: VLESS и прокси

Парк может работать на VLESS-нодах (`all_nodes.txt`), на обычных HTTP(S)/SOCKS5-прокси
(`all_proxys.txt`) или на обоих сразу. В `vless_accounts.env` для аккаунта пишется любая из
ссылок: `account_N = vless://...` или `account_N = http://user:pass@host:port`.

- **Пул прокси** — `all_proxys.txt`, по одному в строке: `user:pass@host:port`,
  `http://user:pass@host:port` или `host:port:user:pass`; необязательный хвост `#us1` задаёт имя и
  страну. Файл секретный (`.gitignore`/`.dockerignore`), `rotation.proxies_file` меняет имя.
- **Выбор источника.** Ключ `rotation.node_sources` в `config.yaml`: `[vless]`, `[proxy]` или
  `[vless, proxy]`. В панели клавиша `n` переключает «оба → только VLESS → только прокси» на лету
  (`state/node_sources.json` сильнее конфига); то же из консоли:
  `python3 scripts/node_sources.py --set proxy|vless|all`, `--clear` возвращает значение из конфига.
- **Что делает переключатель.** mihomo перезапускать не нужно: в нём всегда загружены узлы обоих
  типов. Аккаунты, чей узел относится к выключенному источнику, ставятся на паузу
  (`state/node_source_disabled.json`, читает планировщик скрапера); новые слоты и замена
  сгоревшего узла берут узлы только из включённых источников.
- **Скорость.** `filter_nodes.py --write` проверяет через каждый узел `reddit.com` (ответ 200),
  замеряет задержку и пишет годные в `all_nodes.reddit_ok.txt` от самых быстрых к медленным.
  Прокси выдаются слотам самыми быстрыми первыми, VLESS — как раньше, с разбросом по странам; если
  включены оба типа, слоты делятся пропорционально числу свободных узлов. Для проверки тысячи
  узлов используйте `--workers 32 --timeout-ms 5000`.
- **Гео прокси.** Если в имени нет кода страны, `filter_nodes.py` определяет её по реальному
  выходному IP (`state/proxy_geo.json`, `--no-geo` отключает) — по ней выбираются locale/timezone.
- Один узел = один аккаунт (уникальный IP): уже назначенные узлы свободными не считаются.
