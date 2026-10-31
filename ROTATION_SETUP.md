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

## Автопривязка прокси к аккаунтам (`attach_proxies.py`)

Отдельный скрипт для сценария «положил прокси в файл — аккаунты сами их получили». `--mode login` (по
умолчанию) создаёт ЛОГИН-слоты: `login: true`, `enabled: false` до входа через noVNC, запись в
`refresh_cookies.yaml`; `--mode guest` — гостевые слоты. Запись «всё или ничего»: файлы сначала
формируются и проверяются в памяти. Прокси лежат в
`all_proxies.txt` (по одному в строке, формат тот же, что у `all_proxys.txt`; файл секретный и в
`.gitignore`/`.dockerignore`). Если его нет, берётся файл из `rotation.proxies_file`. VLESS-ноды из
`all_nodes.txt` участвуют в подборе наравне с прокси (`--sources proxy` — только прокси). Без `--apply`
печатается только план; перед записью делаются `*.bak-attach-<время>`; на время запуска остановите
`rotation-daemon`.

```bash
venv/bin/python3 scripts/attach_proxies.py rank                  # очередь прокси по скорости, без записи
venv/bin/python3 scripts/attach_proxies.py add --count 10        # план: 10 новых гостевых аккаунтов
venv/bin/python3 scripts/attach_proxies.py add --count 10 --apply
```

- **Скорость.** Каждый кандидат проверяется запросом к `https://www.reddit.com/robots.txt` через сам
  прокси (нужен HTTP 200); mihomo для этого не нужен. Главную и `.json` без cookies Reddit отдаёт анониму
  с 403 даже через рабочий прокси, поэтому замер проверяет доступность и скорость, а пустит ли Reddit в
  браузер, видно при входе через noVNC (дальше `replace`); другой URL — `--probe-url`. VLESS проверяются delay-API mihomo. Аккаунты получают
  узлы от самых быстрых к самым медленным, неизмеренные VLESS — в самом конце.
- **Время проверки.** Прокси проверяются параллельно (`--workers`, по умолчанию 100) с таймаутом
  `--timeout-ms` (6000): основная трата времени — прокси, которые не отвечают, поэтому таймаут короче =
  быстрее. Замеры кэшируются на `--cache-minutes` (30; `0` — без кэша) в `state/proxy_speed.json`
  (только хэши, без учётных данных), так что `replace` сразу после `add` почти не ждёт.
- **Что создаётся.** Слот (порт, `accounts.yaml`; login — `refresh_cookies.yaml`, guest —
  `refresh_guest_cookies.yaml` и `rotation.slots`), строка `account_N = ...` в `vless_accounts.env` и профиль устройства
  `state/profiles/account_N.json`; гео (locale/timezone) определяется по выходному IP прокси. Гостевым
  слотам без узла сначала достаётся узел, потом создаются новые аккаунты.
- **Капча или Reddit недоступен** (после попытки входа через `login_hub.py --accounts ...`):

  ```bash
  venv/bin/python3 scripts/attach_proxies.py replace --accounts 12 13 --apply
  venv/bin/python3 scripts/attach_proxies.py replace --from-hub --apply   # у кого hub показал капчу
  venv/bin/python3 scripts/attach_proxies.py replace --auto --apply       # проверить доступность Reddit через текущие прокси
  ```

  Для аккаунта берётся следующий по скорости свободный узел, генерируется другое устройство (старый
  профиль уходит в `state/profiles/.history/`), старый узел попадает в чёрный список
  (`state/proxy_attach.json` и `bad_nodes` реестра демона, по умолчанию на 72 ч, `--bad-ttl-hours`).
  В режиме login аккаунты с сохранённой сессией (`storage_state/`) пропускаются: замена сбросит вход
  (`--force`, если так и надо). Cookies после замены нужно получить заново.
- **Профили только десктопные** (`--allow-mobile` разрешает Android/iOS). Профиль устройства
  `state/profiles/account_N.json` главнее `accounts.yaml`: именно его берут `login_hub.py` и скрапер,
  поэтому `rotate_fingerprints.py` на уже созданный профиль не влияет. Пересоздать профиль без смены
  прокси: `attach_proxies.py reprofile --accounts 56-65 --apply` (по умолчанию только мобильные;
  `--all` — любой; аккаунты с сохранённой сессией пропускаются без `--force`).
- После `--apply`: `generate_mihomo_config.py` + `docker compose restart mihomo`, вход через
  `login_hub.py`, перезапуск `rotation-daemon` и `scraper` (скрипт печатает эти шаги).
- Чтобы демон ротации и mihomo знали весь пул (а не только выданные прокси), укажите
  `rotation.proxies_file: all_proxies.txt` в `config.yaml`.

## Регистрация нового аккаунта: ник/пароль и твоя логика (`--register`)

`login_hub.py` и `manual_login.py` принимают `--register`. Хаб/скрипт сам делает всё «вокруг»:
браузер с профилем слота через прокси слота, генерация ника и пароля, открытие `/register`,
проверка входа каждые ~3 с, сохранение cookies/`storage_state`, включение слота.

Ник (`Adjective_Noun123`, проверка `username_available` по прокси слота) и пароль (16 символов)
печатаются в консоль один раз и лежат в `credentials/<account>.json` (каталог 0700, файл 0600;
в `.gitignore`/`.dockerignore`; не в `state/`, логах и `status.json`). Повторный запуск для того же
аккаунта берёт прежние данные, `--new-creds` — генерирует новые.

**Заполнение формы — твоя логика в `scripts/registration_hook.py`:**
`on_page_ready(page, ctx)` — один раз после открытия `/register`; `on_tick(page, ctx)` — каждые ~3 с
до обнаружения входа. В `ctx`: `username`, `password`, `account`, `base_url`, `proxy_url`,
`state` (хранилище шагов между тиками), `log()`, `save_creds(email=...)`. Исключения хука не роняют
сессию (после 20 подряд ошибок `on_tick` отключается). Пока функции пустые, всё делается руками в noVNC.

**Встроенная часть хука.** С `--register` во второй вкладке открывается `mailgenerator.net`: сайт сам
создаёт ящик, хук читает адрес из `#activeEmail` (и пароль ящика из `#loginWrap`) и сохраняет их в
`credentials/<account>.json`; вкладка остаётся открытой — в ней приходят письма. Браузер открывает главную reddit.com; хук нажимает синюю кнопку «Log In» (если она есть), затем «Sign up»
(кнопки ищутся по тексту: Log In / Sign up, также de/ru), после чего `on_tick`
сам проходит шаги формы Reddit: почта → **код из письма** (хук читает его из вкладки почты, подставляет в поле, переключает браузер на
вкладку Reddit и печатает ОДНУ строку `>>> account_96: код подставлен (482913) — регистрация продолжается автоматически
http://…/vnc.html…` со ссылкой noVNC этого аккаунта; затем сам жмёт Enter) → ник и пароль (капчу решаешь ты) →
дата рождения (случайная взрослая, сохраняется в credentials). Пол, интересы и прочие шаги онбординга —
вручную. Селекторы взяты из снимков реальной регистрации и привязаны к `slot`/`id`, не к тексту; если Reddit
поменяет разметку, пришли свежие снимки. Выключатель почты — `MAILBOX_ENABLED` в хуке. Консоль тихая: только строки «нужен ты», повторы и замена ноды;
снимки шагов и смену шагов можно включить через `VERBOSE = True` в хуке, ник — `VERBOSE_REGISTER` в `login_hub.py`;
пароль в консоль не печатается (он в `credentials/<account>.json`).

**Ошибка отправки кода и смена ноды/профиля.** Если после «Продолжить» Reddit пишет «Beim Senden des
Verifizierungscodes ist etwas schiefgelaufen» (баннер под формой e-mail) или форма почты не сменилась на шаг кода
за 15 с после клика, хук жмёт кнопку снова;
после 3 неудач подряд хаб (и `manual_login.py --register`) закрывает браузер и вызывает
`scripts/reidentify.py`: свободная годная нода из `all_nodes.reddit_ok.txt` (проверяется живость через mihomo),
новый impersonate/user-agent/locale, старая нода уходит в `bad`. Правятся `vless_accounts.env`, `accounts.yaml`,
`scripts/refresh_cookies.yaml` (бэкапы `*.bak-reident-*`); `state/profiles/<слот>.json` переименовывается.
Регистрация стартует заново с теми же ником и паролем (новый ящик на mailgenerator.net), максимум 3 замены,
потом слот останавливается. Вручную: `python scripts/reidentify.py account_96` (план) и `--apply` (запись).
Если запущен `rotation-daemon`, после замены перезапусти его — иначе он по реестру вернёт старую ноду.

Баннер cookies («Optionale Cookies ablehnen» / «Reject non-essential») хук отклоняет сам.

**Снимки шагов.** Хук автоматически сохраняет каждый новый шаг формы в
`state/registration_dump/<account>/NN-*.html` (DOM с shadow DOM и iframe, без значений полей и
скриптов) и `.png` (до `MAX_DUMPS`=40 за сессию). Пройди регистрацию один раз руками и пришли
файлы `NN-*.html` — по ним пишутся селекторы. Скриншоты могут показывать введённые данные:
не отправляй папку целиком, не публикуй.

```bash
python scripts/login_hub.py --register ...          # те же аргументы, что и без --register
python scripts/manual_login.py account_60 --register
python scripts/manual_login.py account_60 --register --new-creds
```

### Обновление куков логин-аккаунтов в несколько потоков

`scripts/refresh_cookies.py` обновляет аккаунты параллельно: `workers: 4` в
`scripts/refresh_cookies.yaml` или `--workers N` (1 — по очереди, как раньше). Каждый аккаунт
получает свой браузер; при N потоках средняя пауза между стартами (`stagger_seconds`, `--stagger`)
делится на N. Ориентир по памяти: ~300–500 МБ на headless-браузер.

```bash
python scripts/refresh_cookies.py --workers 8 --stagger 20
```


## Подписка на VLESS-ноды: суточная синхронизация (`sync_subscription.py`)

Подписка — текстовый список `vless://`-ссылок. Узел определяется **меткой** — первым сегментом пути
(`path=/de2/` -> `de2`; нет пути — начало `#имени` до точки). За аккаунтом закреплена конкретная метка (та, что в его
ссылке в `vless_accounts.env`); пока она есть в подписке, аккаунт остаётся на ней.

1. Положи ссылку на подписку в `subscription.url` в корне проекта (одна строка; файл вне git и вне docker-образа) или в
   переменную `NODE_SUBSCRIPTION_URL`. Ссылка содержит токен — секрет: скрипт её не печатает и нигде не сохраняет.
2. План (ничего не пишет): `venv/bin/python3 scripts/sync_subscription.py`.
3. Применить: `venv/bin/python3 scripts/sync_subscription.py --apply`.
4. Раз в сутки: `scripts/install_systemd.sh` ставит `sync-subscription.timer` (юнит запускается, только если есть
   `subscription.url`).

Что происходит при изменениях:
- метка на месте, но ссылка поменялась (путь, uuid, хост) — ссылка аккаунта обновляется, имя в mihomo и привязка к метке те же;
- метки в подписке больше нет — аккаунт получает свободный узел **той же страны** (сначала из `all_nodes.reddit_ok.txt`,
  затем по порядку меток). Другая страна не берётся (locale/часовой пояс профиля привязаны к стране); нет замены — аккаунт
  остаётся как есть, в отчёте предупреждение. `--cross-country` разрешает узел любой страны;
- пул `all_nodes.txt`: ссылки подписки заменяются свежими, прочие узлы сохраняются;
- если что-то изменилось: бэкапы `*.bak-sub-<время>` (последние 5), `generate_mihomo_config.py`, затем
  `docker compose restart mihomo` (`--no-restart` / `--restart-cmd` меняют это). Ошибка генератора откатывает файлы.

Предохранитель: подписка с числом узлов меньше `--min-nodes` (10) или меньше половины ранее виденных — сбой провайдера,
файлы не меняются (код выхода 2). Состояние (метки и серверы) — `state/subscription_state.json`. После смены ссылок
аккаунтов перезапусти `rotation-daemon` (реестр хранит ключи нод слотов). Мёртвую ноду между суточными прогонами скрипт
не ловит — это по-прежнему делают заявки демона и `filter_nodes.py`.

## Бесплатные прокси (ProxyScrape)

`scripts/fetch_free_proxies.py` скачивает список `protocol://ip:port` (по умолчанию ProxyScrape v4,
`proxy_format=protocolipport`), проверяет доступность и пишет лучшие в `all_proxys.free.txt` рядом с
`all_nodes.txt`. Файл входит в общий пул (`rotation.load_pools`): его видят `generate_mihomo_config.py`,
`filter_nodes.py`, `add_login_slots.py` и демон ротации как источник «proxy» (имена `free-<ip>-<порт>`).

- Протоколы: `http`, `https` (как HTTP-прокси) и `socks5`; `socks4` пропускается (mihomo не умеет исходящий socks4).
- Проверка (напрямую, 200 потоков, таймаут 8 с, сертификаты проверяются): туннель до
  `cloudflare.com/cdn-cgi/trace`, затем `https://www.reddit.com/` — ждём 200, как в `filter_nodes.py`
  (`--lenient` — любой ответ < 500). Остаются `--max` (300) самых быстрых: каждый узел попадает во ВСЕ группы mihomo.
- Без `--apply` — только отчёт; пустой результат существующий файл не затирает.

```bash
venv/bin/python3 scripts/fetch_free_proxies.py                 # отчёт
venv/bin/python3 scripts/fetch_free_proxies.py --apply         # запись all_proxys.free.txt
venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo
venv/bin/python3 scripts/filter_nodes.py --write               # финальная проверка через mihomo
```

Убрать бесплатные прокси из пула: удалить `all_proxys.free.txt` и перегенерировать конфиг mihomo.
Бесплатные прокси живут часы — обновляй список чаще. Не ставь на них логин-аккаунты с ценными сессиями:
`add_login_slots.py` выдаёт слотам любые годные узлы источника «proxy», включая эти.
