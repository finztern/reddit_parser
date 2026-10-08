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
sudo cp scripts/systemd/rotation-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rotation-daemon
journalctl -u rotation-daemon -f
venv/bin/python3 scripts/rotation_daemon.py --status
docker compose up -d --build scraper
```
