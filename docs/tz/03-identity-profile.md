# Модуль 03 — постоянный профиль устройства, гео из IP

## Цель
У каждого аккаунта один раз создаётся «устройство», сохраняется и **больше не меняется** (кроме явного сброса). Locale, часовой пояс
и язык вытаскиваются из выходного IP ноды автоматически, руками не пишутся.

## Профиль (`state/profiles/account_N.json`, создаётся при первом входе)
| Группа | Поля | Где применяется |
|---|---|---|
| Сеть | `impersonate` (TLS), `user_agent`, `sec_ch_ua*`, `accept_language`, `platform` | каждый запрос `curl_cffi` (скрапер) и браузер |
| Гео | `country`, `locale`, `timezone_id`, `languages[]`, `geo_source`, `exit_ip`, `checked_at` | браузер + `Accept-Language` |
| Экран | `screen {w,h}`, `viewport`, `device_pixel_ratio`, `color_depth` | только браузер |
| Железо | `hardware_concurrency`, `device_memory_gb`, `webgl_vendor`, `webgl_renderer` | только браузер (init-скрипт) |
| Прочее | `engine`, `channel`, `fonts_preset`, `created_at`, `seed` | браузер |

## Требования
1. **Генератор** `scraper/profile_gen.py`: детерминированно от `seed`, берёт **согласованный пресет устройства**
   (Windows 10/11 + Chrome + типичные NVIDIA/Intel/AMD GPU и разрешения; macOS + Safari/Chrome + Apple GPU; Android + мобильный экран).
   Пресеты — таблица в `scraper/profile_presets.yaml` (разрешения 1366×768, 1536×864, 1920×1080, 2560×1440 и т. д., ядра 4–16, RAM 4–32).
   Распределение по парку разнообразное, но реалистичное; без экзотики.
2. **Гео из IP**: `scraper/geoip.py` — запрос выходного IP **через прокси слота** (ipinfo/ip-api/или локальная MaxMind-база, если есть),
   выдаёт `country → locale/timezone/languages`. Кэш в профиле, перепроверка при смене ноды. Таблицу `COUNTRY_GEO` расширить до всех стран
   нод; неизвестная страна → `en-US` + пометка в панели, а не молчаливый дефолт.
3. **Согласованность**: проверка `validate_profile()` — UA ↔ platform ↔ GPU ↔ экран ↔ TLS-отпечаток; несогласованный профиль не сохраняется.
4. **Применение**:
   - `scraper/http_client.py`: UA, `sec-ch-ua`, `Accept-Language` берутся из профиля (сейчас — из `accounts.yaml`/`geo.py`);
   - Playwright: `viewport/screen/locale/timezone_id/user_agent` + init-скрипт для `hardwareConcurrency`, `deviceMemory`, WebGL vendor/renderer.
5. **Неизменность**: профиль read-only после создания; смена возможна только командой `profile.py --reset account_N` (с предупреждением:
   сессия может слететь).
6. Миграция: для `account_1..10` профиль собирается из текущих `accounts.yaml`, без смены отпечатка.

## Приёмка
- 64 профиля: нет двух полностью одинаковых, все проходят `validate_profile()`.
- Профиль не меняется между перезапусками; скрапер и браузер используют одно и то же значение UA/языка.
- Locale/timezone совпадают со страной выходного IP (тест на заглушке GeoIP).

## Не входит
Подмена на сетевом уровне того, что в сеть не попадает; обход детекта браузера специальными патчами.
