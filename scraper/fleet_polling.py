"""
Модуль 05 — планировщик опроса парка: конфиг и машина состояний аккаунта.

Здесь только чистая логика (без asyncio и без сети), чтобы правила были простыми
и тестировались на синтетических событиях. Время всегда передаётся снаружи
(`now` — monotonic-секунды), поэтому тесты идут на симуляторе времени.

Состояния аккаунта:

  active       работает в общей карусели
  cooldown     аварийный тормоз: 429 / Retry-After / аномальные X-Ratelimit-*;
               пауза растёт экспоненциально (cooldown_base .. cooldown_max)
  degraded     частые пустые листинги подряд; опрашивается только по своему
               (растущему) кулдауну, в карусель возвращается после
               degraded_recover_ok успешных опросов подряд
  auth_failed  401/403 подряд (auth_failed_after) — нужен вход; не опрашивается,
               раз в recovery_probe_seconds — одна проба (на случай новых cookies)
  blocked_node нода забанена — ждём замену ноды демоном; пробы как у auth_failed
  disabled     выключен оператором (fleet_polling.disabled_accounts)

Переходы:

  ok            -> active (из cooldown/degraded — после degraded_recover_ok успехов
                   для degraded; из cooldown — сразу)
  empty x N     -> degraded (N = degraded_after_empty)
  429/5xx       -> cooldown, пауза = max(base * 2^(streak-1), Retry-After), не больше max
  401/403 x N   -> auth_failed (N = auth_failed_after)
  node banned   -> blocked_node (ставит воркер, когда подал заявку на замену ноды)
  новые cookies / замена ноды -> active (счётчики обнуляются)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

ACTIVE = "active"
COOLDOWN = "cooldown"
DEGRADED = "degraded"
AUTH_FAILED = "auth_failed"
BLOCKED_NODE = "blocked_node"
DISABLED = "disabled"

ALL_STATES = (ACTIVE, COOLDOWN, DEGRADED, AUTH_FAILED, BLOCKED_NODE, DISABLED)

# Состояния, в которых аккаунт не участвует в карусели (только редкие пробы)
OUT_OF_ROTATION = (AUTH_FAILED, BLOCKED_NODE, DISABLED)

MODE_ROUND_ROBIN = "round_robin"


@dataclass(frozen=True)
class FleetPollingConfig:
    """Секция `fleet_polling` из config.yaml (читается на лету, значения нормализуются)."""

    mode: str = MODE_ROUND_ROBIN
    pause_min_seconds: float = 3.0
    pause_max_seconds: float = 7.0
    jitter_ratio: float = 0.2
    target_fleet_rate: float | None = None   # None = auto (по перекрытию страниц)
    cooldown_base_seconds: float = 30.0
    cooldown_max_seconds: float = 900.0
    auth_failed_after: int = 3
    # --- пороги переходов (в конфиге, значения по умолчанию разумные) ---
    degraded_after_empty: int = 3
    degraded_recover_ok: int = 2
    empty_cooldown_base_seconds: float = 3.0
    empty_cooldown_max_seconds: float = 60.0
    recovery_probe_seconds: float = 120.0
    ratelimit_emergency_remaining: int = 3
    disabled_accounts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def mean_pause(self) -> float:
        return (self.pause_min_seconds + self.pause_max_seconds) / 2.0

    def pick_pause(self, rng=random) -> float:
        """Нижняя граница паузы аккаунта на ОДИН запрос: случайно pause_min..pause_max."""
        if self.pause_max_seconds <= 0:
            return 0.0
        return rng.uniform(self.pause_min_seconds, self.pause_max_seconds)

    @classmethod
    def from_getter(cls, get) -> "FleetPollingConfig":
        """`get` — ConfigStore.get. Мусор в конфиге не роняет планировщик: берём дефолты."""
        raw = get("fleet_polling", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        d = cls()

        def num(key, default, lo=0.0):
            try:
                v = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return v if v >= lo else default

        def integer(key, default, lo=1):
            try:
                v = int(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return v if v >= lo else default

        # обратная совместимость: старый ключ stream_jitter_ratio, пока в fleet_polling нет своего
        try:
            legacy_jitter = float(get("stream_jitter_ratio", d.jitter_ratio))
        except (TypeError, ValueError):
            legacy_jitter = d.jitter_ratio
        if legacy_jitter < 0:
            legacy_jitter = d.jitter_ratio

        pmin = num("pause_min_seconds", d.pause_min_seconds)
        pmax = num("pause_max_seconds", d.pause_max_seconds)
        if pmax < pmin:
            pmax = pmin

        rate = raw.get("target_fleet_rate", "auto")
        if isinstance(rate, str) or rate is None:
            target = None  # "auto" или что-то нечисловое
        else:
            try:
                target = float(rate)
            except (TypeError, ValueError):
                target = None
            if target is not None and target <= 0:
                target = None

        base = num("cooldown_base_seconds", d.cooldown_base_seconds, lo=0.1)
        cmax = max(base, num("cooldown_max_seconds", d.cooldown_max_seconds, lo=0.1))

        mode = str(raw.get("mode", MODE_ROUND_ROBIN))
        if mode != MODE_ROUND_ROBIN:
            mode = MODE_ROUND_ROBIN  # других режимов нет — не падаем на опечатке

        disabled = raw.get("disabled_accounts") or []
        if not isinstance(disabled, (list, tuple)):
            disabled = []

        return cls(
            mode=mode,
            pause_min_seconds=pmin,
            pause_max_seconds=pmax,
            jitter_ratio=min(0.9, num("jitter_ratio", legacy_jitter)),
            target_fleet_rate=target,
            cooldown_base_seconds=base,
            cooldown_max_seconds=cmax,
            auth_failed_after=integer("auth_failed_after", d.auth_failed_after),
            degraded_after_empty=integer("degraded_after_empty", d.degraded_after_empty),
            degraded_recover_ok=integer("degraded_recover_ok", d.degraded_recover_ok),
            empty_cooldown_base_seconds=num("empty_cooldown_base_seconds", d.empty_cooldown_base_seconds, lo=0.1),
            empty_cooldown_max_seconds=num("empty_cooldown_max_seconds", d.empty_cooldown_max_seconds, lo=0.1),
            recovery_probe_seconds=num("recovery_probe_seconds", d.recovery_probe_seconds, lo=1.0),
            ratelimit_emergency_remaining=integer("ratelimit_emergency_remaining",
                                                  d.ratelimit_emergency_remaining, lo=0),
            disabled_accounts=tuple(str(x) for x in disabled),
        )


def exp_cooldown(streak: int, base: float, cap: float) -> float:
    """base * 2^(streak-1), не больше cap. streak >= 1."""
    if streak <= 0:
        return 0.0
    # 2 ** 60 с запасом, чтобы не плодить огромные int при долгих сериях
    return min(cap, base * (2 ** min(streak - 1, 60)))


class AccountHealth:
    """Состояние одного аккаунта. Не знает про asyncio и сеть; время — параметр `now`."""

    __slots__ = ("name", "state", "since", "cooldown_until", "rate_streak", "empty_streak",
                 "auth_streak", "ok_streak", "last_auth_status", "last_error", "last_error_at",
                 "last_ok_at", "transitions", "_rng")

    def __init__(self, name: str, now: float = 0.0, rng=random):
        self.name = name
        self.state = ACTIVE
        self.since = now
        self.cooldown_until = 0.0
        self.rate_streak = 0      # подряд 429/5xx
        self.empty_streak = 0     # подряд пустых листингов
        self.auth_streak = 0      # подряд 401/403
        self.ok_streak = 0        # подряд успешных (для выхода из degraded)
        self.last_auth_status: int | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_ok_at: float | None = None
        self.transitions: list[tuple[float, str, str]] = []  # (now, from, to) — для тестов/журнала
        self._rng = rng

    # ---- служебное ----
    def _set(self, state: str, now: float):
        if state != self.state:
            self.transitions.append((now, self.state, state))
            if len(self.transitions) > 50:
                del self.transitions[:-50]
            self.state = state
            self.since = now

    def _jitter(self, seconds: float, ratio: float) -> float:
        if ratio <= 0:
            return seconds
        return seconds * self._rng.uniform(1.0 - ratio, 1.0 + ratio)

    def _err(self, kind: str, now: float):
        self.last_error = kind
        self.last_error_at = now

    def effective_state(self, now: float) -> str:
        """cooldown, у которого пауза закончилась, — уже active (аккаунт снова годен)."""
        if self.state == COOLDOWN and now >= self.cooldown_until:
            return ACTIVE
        return self.state

    def in_rotation(self, now: float) -> bool:
        """Участвует ли аккаунт в раздаче темпа парка (считается в N_healthy)."""
        return self.effective_state(now) == ACTIVE

    # ---- события ----
    def on_ok(self, now: float, cfg: FleetPollingConfig):
        self.last_ok_at = now
        self.rate_streak = 0
        self.empty_streak = 0
        self.auth_streak = 0
        self.last_auth_status = None
        self.ok_streak += 1
        if self.state == DEGRADED:
            if self.ok_streak >= cfg.degraded_recover_ok:
                self._set(ACTIVE, now)
        elif self.state != DISABLED:
            # cooldown / auth_failed / blocked_node: проба прошла — аккаунт жив
            self._set(ACTIVE, now)

    def on_rate_limited(self, now: float, cfg: FleetPollingConfig,
                        retry_after: float | None = None) -> float:
        """429 / 5xx / Retry-After. Возвращает паузу аккаунта (сек)."""
        self.rate_streak += 1
        self.ok_streak = 0
        self._err("rate_or_server", now)
        pause = exp_cooldown(self.rate_streak, cfg.cooldown_base_seconds, cfg.cooldown_max_seconds)
        pause = min(cfg.cooldown_max_seconds, self._jitter(pause, cfg.jitter_ratio))
        if retry_after:
            pause = max(pause, float(retry_after))  # Retry-After сервера главнее потолка
        if self.state not in OUT_OF_ROTATION:
            self.cooldown_until = now + pause
            if self.state != DEGRADED:
                self._set(COOLDOWN, now)
        return pause

    def on_emergency_ratelimit(self, now: float, cfg: FleetPollingConfig, ratelimit: dict | None,
                               max_interval: float | None = None) -> float:
        """Аномальные X-Ratelimit-*: остаток почти исчерпан. 0 — если всё нормально.

        Обычные значения заголовков НЕ влияют на темп (темп диктует поток, а не лимиты) —
        это только аварийный тормоз."""
        if not ratelimit:
            return 0.0
        try:
            remaining = float(ratelimit["remaining"])
            reset = float(ratelimit["reset"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        if remaining > cfg.ratelimit_emergency_remaining:
            return 0.0
        pause = reset + 1.0 if reset > 0 else cfg.cooldown_base_seconds
        cap = max_interval if max_interval else cfg.cooldown_max_seconds
        pause = min(pause, cap)
        self.rate_streak += 1
        self.ok_streak = 0
        self._err("ratelimit_low", now)
        if self.state not in OUT_OF_ROTATION:
            self.cooldown_until = now + pause
            if self.state != DEGRADED:
                self._set(COOLDOWN, now)
        return pause

    def on_empty(self, now: float, cfg: FleetPollingConfig) -> float:
        """200 + пустой листинг на первой странице. Возвращает паузу аккаунта."""
        self.empty_streak += 1
        self.ok_streak = 0
        self._err("empty_listing", now)
        pause = exp_cooldown(self.empty_streak, cfg.empty_cooldown_base_seconds,
                             cfg.empty_cooldown_max_seconds)
        pause *= self._rng.uniform(0.8, 1.3)
        if self.empty_streak >= cfg.degraded_after_empty and self.state not in OUT_OF_ROTATION:
            self._set(DEGRADED, now)
        return pause

    def on_auth_error(self, now: float, cfg: FleetPollingConfig, status: int | None) -> str:
        """401/403. Возвращает новое состояние."""
        self.auth_streak += 1
        self.ok_streak = 0
        self.last_auth_status = status
        self._err(f"auth_{status}" if status else "auth", now)
        if self.auth_streak >= cfg.auth_failed_after and self.state not in (BLOCKED_NODE, DISABLED):
            self._set(AUTH_FAILED, now)
        return self.state

    def on_network_error(self, now: float):
        self.ok_streak = 0
        self._err("network", now)

    def mark_blocked_node(self, now: float):
        """Воркер подал заявку на замену ноды — до её выполнения аккаунт не опрашивается."""
        if self.state != DISABLED:
            self._set(BLOCKED_NODE, now)

    def reset(self, now: float, reason: str = ""):
        """Новые cookies / нода заменена / профиль применён: начинаем с чистого листа."""
        self.rate_streak = self.empty_streak = self.auth_streak = self.ok_streak = 0
        self.last_auth_status = None
        self.cooldown_until = 0.0
        if self.state != DISABLED:
            self._set(ACTIVE, now)

    def set_disabled(self, now: float, disabled: bool):
        if disabled:
            self._set(DISABLED, now)
        elif self.state == DISABLED:
            self.state = ACTIVE          # выход из disabled — единственный способ, reset() его не снимает
            self.since = now
            self.transitions.append((now, DISABLED, ACTIVE))
            self.reset(now)

    def probe_pause(self, cfg: FleetPollingConfig) -> float:
        """Пауза между пробами для аккаунтов вне карусели."""
        return self._jitter(cfg.recovery_probe_seconds, cfg.jitter_ratio)