"""Ротация сабов для запроса: каждый аккаунт опрашивает не весь `stream_subs`, а «окно» из k сабов.

Окна у аккаунтов разные (у каждого свой детерминированный порядок сабов) и сдвигаются на каждом опросе,
поэтому соседние запросы парка почти не пересекаются: меньше дублей, разнообразнее запросы.
Чистая функция: без ввода-вывода, результат зависит только от аргументов."""

from __future__ import annotations

import random
import zlib


def pick_subs(subs: list[str], per_request: int, account: str, poll_no: int) -> list[str]:
    """Вернуть k сабов для опроса №poll_no аккаунта `account`.

    per_request <= 0 или >= len(subs) — берётся весь список (старое поведение).
    Иначе: список перемешивается детерминированно по имени аккаунта, окно длины k идёт по кругу
    с шагом k за опрос — за ceil(n/k) опросов аккаунт обходит все сабы."""
    subs = [s for s in subs if s]
    n = len(subs)
    k = int(per_request or 0)
    if k <= 0 or k >= n:
        return subs
    order = sorted(subs)  # стабильная база: порядок в конфиге на выбор не влияет
    random.Random(zlib.crc32(account.encode("utf-8"))).shuffle(order)
    start = (int(poll_no) * k) % n
    return [order[(start + i) % n] for i in range(k)]
