"""
Обмен состоянием между воркером (контейнер) и демоном ротации (хост)
через каталог state/ (в docker-compose монтируется на запись).

Файлы:
  state/requests/account_N.json  заявка воркера на ротацию. Статусы:
        pending  — воркер просит ротацию (воркер на паузе)
        working  — демон работает (воркер на паузе)
        done / rejected — результат; воркер забирает его и удаляет файл
        (поле retry_after — не просить новую ротацию раньше этого времени)
  state/slots/account_N.json     АКТИВНЫЙ профиль слота (impersonate, UA,
        нода, гео). Воркер читает его при старте и на лету — поэтому
        рестарты контейнера не откатывают слот на accounts.yaml.
  state/identities.json          реестр идентичностей — пишет только демон.

Всё пишется атомарно (os.replace в той же директории).
"""

import json
import os
import time
from pathlib import Path

from .constants import BASE_DIR, log

STATE_DIR = Path(os.environ.get("STATE_DIR", BASE_DIR / "state"))
SLOTS_DIR = STATE_DIR / "slots"
REQUESTS_DIR = STATE_DIR / "requests"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("не прочитал %s: %s", path, e)
        return None


def write_json_atomic(path: Path, data) -> None:
    """Контейнер (root) и демон (обычный пользователь) пишут в одни и те
    же каталоги — даём права на запись всем, иначе один не заменит файл
    другого."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o777)
    except OSError:
        pass
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o666)
    except OSError:
        pass
    os.replace(tmp, path)


def slot_profile_path(name: str) -> Path:
    return SLOTS_DIR / f"{name}.json"


def request_path(name: str) -> Path:
    return REQUESTS_DIR / f"{name}.json"


def read_slot_profile(name: str) -> dict | None:
    p = read_json(slot_profile_path(name))
    return p if p and p.get("impersonate") else None


def read_request(name: str) -> dict | None:
    return read_json(request_path(name))


def write_request(name: str, reason: str, streak: int) -> None:
    write_json_atomic(request_path(name), {
        "status": "pending", "ts": time.time(), "reason": reason, "empty_streak": streak,
    })


def clear_request(name: str) -> None:
    try:
        request_path(name).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("не удалось удалить заявку %s: %s", name, e)


class SlotProfileWatcher:
    """Следит за state/slots/account_N.json по mtime (как CookieFileWatcher)."""

    def __init__(self, name: str, min_check_interval: float = 10.0):
        self.path = slot_profile_path(name)
        self.min_check_interval = min_check_interval
        self._mtime: int | None = None
        self._last = 0.0

    def load_if_changed(self, force: bool = False) -> dict | None:
        now = time.monotonic()
        if not force and (now - self._last) < self.min_check_interval:
            return None
        self._last = now
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            return None
        if m == self._mtime:
            return None
        prof = read_slot_profile_path(self.path)
        if prof is None:
            return None
        self._mtime = m
        return prof


def read_slot_profile_path(path: Path) -> dict | None:
    p = read_json(path)
    return p if p and p.get("impersonate") else None
