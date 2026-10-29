from datetime import datetime, timezone


# ---------------------------------------------------------------- #
#  Вспомогательные функции
# ---------------------------------------------------------------- #

def iso_utc(ts: float) -> str:
    """ISO 8601, UTC, секунды без долей: 2026-06-28T01:35:08Z (дробная часть ts отбрасывается)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
