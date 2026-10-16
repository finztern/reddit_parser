# Ротация идентичностей: что поменять в остальных файлах

## 1. docker-compose.yml и docker-compose.bridge.yml (сервис scraper, volumes)
```yaml
      - ./state:/app/state          # НА ЗАПИСЬ (cookies остаётся :ro)
```
`mkdir -p state && chmod 777 state` на хосте (контейнер root, демон — sashko).

## 2. config.yaml — добавить секцию
```yaml
rotation:
  enabled: true
  slots: [account_11, account_12, account_13, account_14, account_15, account_16, account_17,
          account_18, account_19, account_20, account_21, account_22, account_23, account_24, account_25]
  empty_streak_trigger: 6        # пустых листингов подряд до заявки
  request_cooldown_seconds: 600  # не просить ротацию чаще
  request_timeout_seconds: 1800  # воркер игнорирует зависшую заявку
  ban_cooldown_hours: 6          # через сколько забаненную идентичность можно вернуть
  max_rotations_per_day: 6
  max_candidates_per_request: 4
  bad_node_ttl_hours: 24
  global_outage_fraction: 0.5    # пусто у стольких слотов сразу -> не ротируем
  global_outage_pause_seconds: 900
  mihomo_api: "http://127.0.0.1:9090"
```

## 3. scraper/http_client.py — в класс CurlSessionHandle добавить метод
```python
    def set_impersonate(self, impersonate: str):
        new = cffi_requests.Session(impersonate=impersonate, proxies=self._proxies)
        self._impersonate = impersonate   # упадёт на неизвестном таргете ДО подмены
        self.current = new
        self.swaps += 1
```

## 4. scripts/rotation.py — extend_with_pool: все ноды во все группы
Заменить блок `for g in config["proxy-groups"]: g["proxies"] = g["proxies"] + added` на:
```python
    all_names = [p["name"] for p in config["proxies"]]
    for g in config["proxy-groups"]:
        g["proxies"] = g["proxies"] + [n for n in all_names if n not in g["proxies"]]
```
Тогда слот может взять и ноду другого слота (демон не отдаёт ноды логин-аккаунтов и занятые).
Нужен `all_nodes.txt` рядом с проектом (хотя бы один vless:// на строку; если пусто — ротация идёт
только между нодами слотов из vless_accounts.env). После правки:
```bash
venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo
```

## 5. scripts/refresh_guest_cookies.py — часовой джоб должен знать про ротацию
В main() сразу после `accounts = [all_accounts[n] for n in sorted(...)]` вставить:
```python
    from scraper import identity as _idn
    _kept = []
    for _a in accounts:
        _req = _idn.read_request(_a["name"])
        if _req and _req.get("status") in ("pending", "working"):
            log.info("[%s] идёт ротация — пропускаю в этом проходе", _a["name"])
            continue
        _p = _idn.read_slot_profile(_a["name"])
        if _p:  # активный профиль после ротации перекрывает accounts.yaml
            _a = {**_a, "impersonate": _p["impersonate"], "user_agent": _p["user_agent"]}
            cfg["guest_accounts"][_a["name"]] = {k: _p[k] for k in
                ("engine", "channel", "device", "user_agent", "impersonate") if k in _p}
            if _p.get("geo"):
                cfg.setdefault("account_geo", {})[_a["name"]] = _p["geo"]
        _kept.append(_a)
    accounts = _kept
    if not accounts:
        log.info("все слоты в ротации — нечего харвестить"); return 0
```

## 6. Запуск
```bash
sudo scripts/install_systemd.sh --enable      # ставит и включает rotation-daemon (и таймеры харвеста)
journalctl -u rotation-daemon -f
venv/bin/python3 scripts/rotation_daemon.py --status
docker compose up -d --build scraper
```


## 7. Логин-only парк (модуль 01)

Все слоты — логин-аккаунты; число слотов = число **годных** нод (Reddit отдаёт 200) минус запас
(`fleet.reserve_nodes: 6`). Гостевой харвест и гостевая ротация выключены (`fleet.guest_enabled: false`),
код остаётся.

Миграция (из корня проекта, на сервере; каждый шаг сначала печатает план, запись — с `--apply`):

```bash
venv/bin/python3 scripts/filter_nodes.py --write              # годные ноды -> all_nodes.reddit_ok.txt, отчёт state/node_report.json
venv/bin/python3 scripts/convert_to_login.py --all --apply    # гостевые слоты -> логин (enabled: false до входа); или --accounts 11-55
venv/bin/python3 scripts/add_login_slots.py --apply           # добор слотов до (годные - запас); порты с 7946
venv/bin/python3 scripts/generate_mihomo_config.py && docker compose restart mihomo
sudo scripts/install_systemd.sh --enable                      # гостевой таймер отключается; включается filter-nodes.timer (раз в сутки)
venv/bin/python3 scripts/manual_login.py --list               # колонка «вход»: нужен вход / нужна нода / проверить вход
venv/bin/python3 scripts/manual_login.py account_N            # вход; после него слот сам получает enabled: true
docker compose up -d --build scraper
```

Замена сгоревшей ноды (`fleet.node_replace_only: true`): воркер логин-слота после серии 403 (или пустых листингов)
кладёт заявку, демон переводит слот в `needs_node`, берёт свободную годную ноду из запаса и переключает её в mihomo.
Отпечаток (профиль), сессия и cookies **не меняются**, браузер не запускается; слот помечается «проверить вход»
(флаг `state/check_login/account_N`, снимается после `manual_login.py` / успешного `refresh_cookies.py`).
Если запаса нет, слот остаётся `needs_node`, демон повторяет попытку, когда `filter_nodes.py` пополнит пул.
Состояние парка: `venv/bin/python3 scripts/rotation_daemon.py --status`.

Откат: файлы `*.bak-login-*` / `*.bak-addlogin-*` (их оставляют скрипты) возвращаются на место, либо `git revert`;
гостевой режим — `fleet.guest_enabled: true` и `sudo scripts/install_systemd.sh --with-guest --enable`.
