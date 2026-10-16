# Развёртывание на новом сервере

Цель: с первого запуска получить **45 гостевых слотов** (`account_11..55`) плюс
логин-аккаунты, если они у тебя есть. Все слоты уже описаны в репозитории
(порты `7891–7945`, отпечатки, профили харвестера, список ротации), поэтому
на сервере остаётся одно: **дать каждому слоту свою ноду**. Это делает
`scripts/add_guest_slots.py`.

Нужно заранее:

| Что | Сколько |
|---|---|
| Linux x86_64 (Ubuntu/Debian), доступ в интернет | — |
| Нод VLESS в `all_nodes.txt` | **слотов + 8 запасных**: для 45 гостей — минимум 53, лучше 60+. Для логин-аккаунтов — ещё по одной на каждый |
| RAM | скрапер + mihomo ≈ 1–2 ГБ, харвест браузерами — до ~4 ГБ на время прогона |
| Локальный пайплайн | `http://127.0.0.1:9000/store_items` (или `STORE_ENDPOINT` в `docker-compose.yml`) |

## 1. Пакеты

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip xvfb util-linux curl
# Docker + compose-плагин: https://docs.docker.com/engine/install/
sudo usermod -aG docker "$USER"      # затем выйти и зайти заново
```

## 2. Код и окружение

```bash
git clone https://github.com/finztern/reddit_parser.git reddit_scraper
cd reddit_scraper

python3 -m venv venv
venv/bin/pip install -r requirements-refresh.txt
sudo venv/bin/playwright install-deps chromium firefox webkit   # системные библиотеки
venv/bin/playwright install chromium firefox webkit             # сами браузеры (в кэш пользователя)

mkdir -p cookies state storage_state
chmod 777 state                      # в state/ пишут и контейнер (root), и демон ротации
```

Проверка, что установленный `curl_cffi` знает все отпечатки из `accounts.yaml`:

```bash
venv/bin/python3 - <<'EOF'
import yaml
from curl_cffi.requests import BrowserType
have = {b.value for b in BrowserType}
bad = [(a['name'], a['impersonate'])
       for a in yaml.safe_load(open('accounts.yaml'))['accounts'] if a['impersonate'] not in have]
print('OK: все отпечатки известны curl_cffi' if not bad else f'НЕИЗВЕСТНЫЕ: {bad}')
EOF
```

Если что-то неизвестно — для гостей: `venv/bin/python3 scripts/rotate_fingerprints.py`
(подберёт отпечатки из установленного `curl_cffi` и синхронно поправит
`accounts.yaml` и `refresh_guest_cookies.yaml`).

## 3. Ноды

1. **Пул** — файл `all_nodes.txt`, по одной ссылке `vless://...` на строку.
2. **Логин-аккаунты** (необязательно) — файл `vless_accounts.env`, по строке на аккаунт:
   `account_1 = vless://...`. Гостевые слоты в этот файл вписывать **не нужно** —
   скрипт ниже сделает это сам. Если логин-аккаунтов нет, файл не создавай.

Оба файла в `.gitignore` и в репозиторий не попадут.

## 4. mihomo (прокси)

```bash
venv/bin/python3 scripts/generate_mihomo_config.py     # на чистом сервере соберёт конфиг из одного пула
docker compose up -d mihomo
curl -s http://127.0.0.1:9090/proxies | head -c 200    # API должен отвечать
```

## 5. Раздать ноды слотам

```bash
# сначала план: какая нода какому слоту, гео, сколько нод останется про запас
venv/bin/python3 scripts/add_guest_slots.py --count 0 --disable-login-without-node
# если всё устраивает — запись (с бэкапами *.bak-addslots-*)
venv/bin/python3 scripts/add_guest_slots.py --count 0 --disable-login-without-node --apply

venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo
```

- `--count 0` — новых слотов не создавать: 11..55 уже есть в репозитории, скрипту
  остаётся раздать им живые ноды (проверка через mihomo), с разбросом по странам.
  Гео слота (`locale`/таймзона для браузера и `Accept-Language`) ставится по стране ноды.
- `--spare 8` (по умолчанию) — столько живых нод скрипт **не трогает**: они нужны демону
  ротации, чтобы менять забаненные идентичности. Если нод меньше, чем слотов + запас,
  скрипт создаст столько, сколько получится, и скажет об этом.
- `--disable-login-without-node` выключает логин-аккаунты, у которых нет ноды в
  `vless_accounts.env` (на сервере без логин-аккаунтов).

**Хочешь больше 45 слотов** — `--count N` создаёт новые (`account_56...`, порты с 7946),
каждому нужна своя нода:

```bash
venv/bin/python3 scripts/add_guest_slots.py --count 20 --apply
```

### Нода жива, но Reddit её не пускает

Reddit банит целые подсети датацентровых VPN: нода проходит проверку живости, а главная
`reddit.com` отдаёт 403 «blocked due to a network policy». Отфильтруй пул по реальному ответу Reddit:

```bash
venv/bin/python3 scripts/filter_nodes.py              # сколько нод Reddit пускает, по странам
venv/bin/python3 scripts/filter_nodes.py --write      # + all_nodes.reddit_ok.txt
venv/bin/python3 scripts/add_guest_slots.py --count 0 --require-reddit --apply   # раздать только годные
```

Если ноды слотам **уже выданы** (а Reddit их не пускает), `--require-reddit` их не заменит —
нужен `--replace-banned`: он проверяет ноды выданных слотов и меняет забаненные на годные
(остальные слоты не трогает, старые строки в `vless_accounts.env` закомментированы):

```bash
sudo systemctl stop rotation-daemon 2>/dev/null          # демон не должен писать state параллельно
venv/bin/python3 scripts/add_guest_slots.py --count 0 --replace-banned --spare 4            # план
venv/bin/python3 scripts/add_guest_slots.py --count 0 --replace-banned --spare 4 --apply
venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo
sudo systemctl start rotation-daemon 2>/dev/null
```

Нужен mihomo Meta (параметр `expected` в delay-API). Если «годными» оказались все ноды,
перепроверь вручную: `curl -x http://127.0.0.1:ПОРТ -o /dev/null -w "%{http_code}\n" https://www.reddit.com/`.

## 6. Cookies гостевых слотов

Параллельный харвест браузерами. В **первый раз** запусти осторожно и смотри память:

```bash
watch -n2 free -m                                          # в соседнем терминале
xvfb-run -a venv/bin/python3 scripts/refresh_guest_cookies.py --no-headless --workers 3
```

Запуск **с окном под `xvfb-run`**, как в systemd-юните: ручной прогон должен вести себя так же, как
таймер, иначе проверка ничего не докажет. Без `--no-headless` браузеры идут в headless-режиме
(`headless: true` в yaml), а Reddit может отвечать таким сессиям отказом.

Если `available` не падает ниже ~1.5 ГБ, дальше запускай без флага — число воркеров
выберется само (потолок и пороги памяти — в `scripts/refresh_guest_cookies.yaml`,
по умолчанию до 6 одновременных браузеров). В конце лога — сводка `успешно=N, ошибок=M` и
причина для каждого слота, который не прошёл. Слот, не прошедший верификацию, повторяется
в этом же прогоне; остальное добивается так:

```bash
venv/bin/python3 scripts/refresh_guest_cookies.py --account account_17 --account account_31
ls cookies/ | wc -l                                         # сколько файлов cookies есть
```

Причину отказа слота показывает последняя строка про него (`не удалось: ...`):

- `порт N не принимает соединения` — браузер даже не запускался: у слота нет слушателя mihomo
  (нет ноды в `vless_accounts.env` либо после `generate_mihomo_config.py` не перезапущен mihomo);
- `ошибка навигации ... — <подсказка>` — прокси-порт, нода или падение браузера (нехватка RAM), подсказка
  называет причину;
- `верификация ... вернул 403 ... ВЫВОД: ...` — страница загрузилась, cookies получены, но JSON не отдан.
  В строке есть статус и заголовки ответа, начало тела и результат **той же проверки из настоящего
  браузера**. «Браузер получает JSON, а curl_cffi нет» — проблема в отпечатке запроса (`impersonate`),
  не в IP и cookies. «JSON закрыт и для браузера» — IP ноды заблокирован Reddit'ом либо изменение на
  стороне Reddit.

## 7. Скрапер

```bash
docker compose up -d --build
docker compose logs -f scraper
docker compose logs scraper | grep -c "Старт"               # должно совпасть с числом включённых слотов
```

В строке старта каждого слота видно `impersonate` и `Accept-Language` — язык должен
соответствовать стране ноды слота.

## 8. Автоматика (systemd)

Юниты — шаблоны без вшитых путей; установщик подставляет пользователя и каталог проекта:

```bash
sudo scripts/install_systemd.sh --enable --no-login     # без логин-аккаунтов
sudo scripts/install_systemd.sh --enable                # если логин-аккаунты есть
systemctl list-timers | grep refresh
journalctl -u rotation-daemon -f
```

Ставятся: `refresh-guest-cookies.timer` (гостевые cookies раз в час, ~2 ч живут),
`rotation-daemon.service` (смена ноды/отпечатка у забаненных слотов) и, без
`--no-login`, `refresh-cookies.timer` (логин-аккаунты раз в неделю). Все браузерные
джобы берут общий `flock`, а при нехватке памяти ядро убивает их первыми (не скрапер).

## 9. Логин-аккаунты (необязательно)

Первый вход делается руками, дальше сессию поддерживает `refresh-cookies.timer`:

```bash
venv/bin/python3 scripts/manual_login.py account_1      # нужны xvfb x11vnc novnc websockify
```

Затем прописать ссылку `account_1 = vless://...` в `vless_accounts.env`, поставить
`enabled: true` в `accounts.yaml`, пересобрать конфиг mihomo (п. 5) и
`docker compose restart scraper`.

Несколько аккаунтов сразу (модуль 02): `login_hub.py` поднимает по сессии на аккаунт —
свой Xvfb/VNC/noVNC на портах `login_hub.port_base` + индекс (6001..6004), свой браузер и
свою ноду. noVNC слушает только `login_hub.bind` и пускает только `login_hub.allowed_cidr`;
пароль VNC генерируется и печатается при запуске.

```bash
venv/bin/python3 scripts/login_hub.py --list-queue                         # кто ждёт входа
venv/bin/python3 scripts/login_hub.py --accounts account_11 account_12 account_13 account_14
venv/bin/python3 scripts/login_hub.py --next 4 --queue                     # 4 из очереди + добор по мере освобождения
```

Статусы сессий (в т.ч. `waiting_user` — капча/письмо) — `state/login_hub/status.json`.
Ctrl+C гасит все сессии без «осиротевших» Xvfb/x11vnc/websockify/chromium.

## Проверка после запуска

```bash
venv/bin/python3 scripts/rotation_daemon.py --status   # слоты, идентичности, живые/мёртвые ноды
docker compose logs --since 10m scraper | grep -c "пустой листинг"
docker stats --no-stream                               # память контейнеров
```

Через сутки посмотри `--status`: если свободных нод в запасе почти не осталось,
слотам при бане некуда ротироваться — пополни `all_nodes.txt` и повтори
`generate_mihomo_config.py` + `docker compose restart mihomo`.

## Если что-то пошло не так

| Симптом | Причина / что делать |
|---|---|
| `add_guest_slots.py`: «пул нод не загружен в запущенный mihomo» | `generate_mihomo_config.py` → `docker compose restart mihomo`, повторить |
| «запас на ротацию мал» | в `all_nodes.txt` мало нод — добавь (слотов + 8 минимум) |
| харвест: `верификация ... вернул 403` у **всех** слотов | причина общая, а не «сломанные ноды». Сначала убедись, что запуск был с окном (`xvfb-run ... --no-headless`), затем читай `ВЫВОД` в строке ошибки (п. 6): отпечаток curl_cffi, блок IP или изменение на стороне Reddit |
| харвест: `верификация ... вернул 403` у **части** слотов | нода/IP этих слотов у Reddit в бане — слот попадёт в ротацию, либо повтори `--account` |
| харвест: `не смог запустить webkit/firefox` | `sudo venv/bin/playwright install-deps` и `venv/bin/playwright install webkit firefox` |
| харвест: «мало свободной RAM» | уменьши `--workers` или `parallel_browsers`; убедись, что скрапер не разросся (`docker stats`) |
| воркер: сетевые ошибки по одному слоту | у слота нет ноды/слушателя mihomo — раздай ноды (п. 5) или выключи слот (`enabled: false`) |
| после `git pull` конфликт в `accounts.yaml` | скрипты правят файлы на месте: закоммить локальные правки (`git add -A && git commit`) до `pull` |
