import logging
import logging.handlers
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config.yaml"))
ACCOUNTS_PATH = Path(os.environ.get("ACCOUNTS_PATH", BASE_DIR / "accounts.yaml"))

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
STORE_ENDPOINT_OVERRIDE = os.environ.get("STORE_ENDPOINT")
# Переопределяет queue_control.url из config.yaml (в bridge-режиме Docker
# 127.0.0.1 внутри контейнера — это не хост, нужен host.docker.internal).
QUEUE_CONTROL_URL_OVERRIDE = os.environ.get("QUEUE_CONTROL_URL")
CONFIG_RELOAD_SECONDS = float(os.environ.get("CONFIG_RELOAD_SECONDS", "5"))

# Жёсткий потолок сервера (см. документацию коллектора: /store_items
# отклоняет пачки больше BATCH_MAX_ITEMS с 413). Наш batch_max_items из
# config.yaml обрезается этим значением на всякий случай.
SERVER_HARD_BATCH_LIMIT = 1000

# Сколько секунд сверх HTTP-таймаута (connect + read) ждать поток
# curl_cffi, прежде чем считать вызов зависшим и разблокировать цикл
# воркера принудительно.
HTTP_EXECUTOR_SLACK_SECONDS = 5

# Дефолтный connect-таймаут curl_cffi, если connect_timeout_seconds не
# задан в config.yaml.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5

# Как часто (в секундах) логировать снапшот занятости http_executor
# (см. scraper/health.py) и проверять условие свопа.
EXECUTOR_HEALTH_LOG_INTERVAL_SECONDS = float(os.environ.get("EXECUTOR_HEALTH_LOG_INTERVAL_SECONDS", "300"))

# ---------------------------------------------------------------- #
#  Своп http_executor при деградации (см. scraper/health.py)
#
#  asyncio.wait_for() не может принудительно прервать физический поток
#  curl_cffi — если прокси/DNS зависают навсегда, поток остаётся
#  "занятым" пулом бесконечно. Единственный способ "вылечиться" без
#  рестарта процесса — пересоздать пул целиком.
# ---------------------------------------------------------------- #

# Доля max_workers, при достижении которой считаем пул почти исчерпанным.
EXECUTOR_SWAP_THRESHOLD = float(os.environ.get("EXECUTOR_SWAP_THRESHOLD", "0.9"))

# Минимальный интервал между двумя свопами подряд.
EXECUTOR_SWAP_COOLDOWN_SECONDS = float(os.environ.get("EXECUTOR_SWAP_COOLDOWN_SECONDS", "300"))

# Если суммарно утёкших потоков >= этот множитель * max_workers — процесс
# завершается (SystemExit), docker перезапустит его чисто.
EXECUTOR_FATAL_LEAK_MULTIPLIER = float(os.environ.get("EXECUTOR_FATAL_LEAK_MULTIPLIER", "10"))

# ---------------------------------------------------------------- #
#  Логирование
#
#  По умолчанию — stdout/stderr (под Docker размер ограничивает
#  logging-driver, см. docker-compose.yml). Если задан LOG_FILE —
#  RotatingFileHandler с потолком LOG_MAX_BYTES x LOG_BACKUP_COUNT.
# ---------------------------------------------------------------- #
LOG_FILE = os.environ.get("LOG_FILE")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10MB
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", "3"))

_log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)

if LOG_FILE:
    _log_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
else:
    _log_handler = logging.StreamHandler()

_log_handler.setFormatter(_log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("scraper")

if LOG_FILE:
    log.info(
        "Логирование в файл с ротацией: %s (max %d байт x %d бэкапов)",
        LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT,
    )
