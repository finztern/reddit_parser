import asyncio
import json
import time
from pathlib import Path

import yaml

from .constants import ACCOUNTS_PATH, log


# ---------------------------------------------------------------- #
#  Живой конфиг (hot-reload)
# ---------------------------------------------------------------- #

class ConfigStore:
    def __init__(self, path: Path, reload_interval: float):
        self.path = path
        self.reload_interval = reload_interval
        self._data: dict = {}

    def load_once(self):
        with open(self.path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    async def reload_loop(self):
        while True:
            await asyncio.sleep(self.reload_interval)
            try:
                old = self._data
                self.load_once()
                if old != self._data:
                    qc = self.get("queue_control", {}) or {}
                    log.info(
                        "config.yaml обновлён: stream_subs=%s max_age=%ss batch_max_items=%s "
                        "queue_control=%s fallback_rate=%s",
                        "+".join(self.get("stream_subs", ["all"])),
                        self.get("max_age_seconds"),
                        self.get("batch_max_items"),
                        "on" if qc.get("enabled") else "off",
                        self.get("target_rate_per_second"),
                    )
            except Exception as e:
                log.warning("Не удалось перечитать config.yaml (оставляю старые значения): %s", e)

    def get(self, key, default=None):
        return self._data.get(key, default)


def load_accounts() -> list[dict]:
    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("accounts", [])


def load_cookies(cookie_file: Path) -> dict:
    with open(cookie_file, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}


# ---------------------------------------------------------------- #
#  Hot-reload cookies на аккаунт
#
#  cookie_file обновляется ВНЕШНИМИ процессами (refresh_cookies.py для
#  логин-аккаунтов, refresh_guest_cookies.py для гостевых) атомарно, через
#  os.replace(). account_worker на каждой итерации дёшево проверяет mtime
#  файла и перечитывает содержимое, только если файл реально поменялся.
# ---------------------------------------------------------------- #

class CookieFileWatcher:
    """Отслеживает изменения cookie_file по mtime.

    min_check_interval — верхний предел частоты самой проверки (os.stat).
    Это ограничение ТОЛЬКО на частоту проверки, а не на задержку
    применения уже обнаруженного изменения."""

    __slots__ = ("path", "min_check_interval", "_mtime", "_last_check")

    def __init__(self, path: Path, min_check_interval: float = 30.0):
        self.path = path
        self.min_check_interval = min_check_interval
        self._mtime: float | None = None
        self._last_check = 0.0

    def load_if_changed(self, force: bool = False) -> dict | None:
        """Возвращает новый dict cookies, если файл изменился (или это
        самый первый вызов с force=True), иначе None.

        Ошибки чтения/парсинга не поднимаются наружу: логируются и
        трактуются как "изменений нет" — старые cookies остаются в силе."""
        now = time.monotonic()
        if not force and self._mtime is not None and (now - self._last_check) < self.min_check_interval:
            return None
        self._last_check = now

        try:
            mtime = self.path.stat().st_mtime
        except OSError as e:
            if self._mtime is not None:
                log.warning("Не удалось получить mtime %s (оставляю старые cookies): %s", self.path, e)
            return None

        if self._mtime is not None and mtime == self._mtime:
            return None

        try:
            cookies = load_cookies(self.path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("Не удалось перечитать cookies из %s (оставляю старые): %s", self.path, e)
            return None

        if not cookies:
            log.warning(
                "%s перечитан, но не содержит ни одной валидной cookie — игнорирую, оставляю старые",
                self.path,
            )
            return None

        self._mtime = mtime
        return cookies
