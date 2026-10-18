"""Ротация сабов для запроса: каждый аккаунт опрашивает не весь `stream_subs`, а «окно» из k сабов.

Окна у аккаунтов разные (у каждого свой детерминированный порядок сабов) и сдвигаются на каждом опросе,
поэтому соседние запросы парка почти не пересекаются: меньше дублей, разнообразнее запросы.
Чистая функция: без ввода-вывода, результат зависит только от аргументов."""

from __future__ import annotations

import random
import zlib


def pick_subs(subs: list[str], per_request: int, account: str, poll_no: int,
              all_chance: float = 1.0) -> list[str]:
    """Вернуть k сабов для опроса №poll_no аккаунта `account`.

    per_request <= 0 или >= len(subs) — берётся весь список (старое поведение).
    Иначе: список перемешивается детерминированно по имени аккаунта, окно длины k идёт по кругу
    с шагом k за опрос — за ceil(n/k) опросов аккаунт обходит все сабы.

    "all" в ротацию не входит: он добавляется в склейку (сверх k сабов) с вероятностью all_chance
    (детерминированно по аккаунту и номеру опроса) — почти всегда, но не постоянно."""
    subs = [s for s in subs if s]
    k = int(per_request or 0)
    if k <= 0 or k >= len(subs):
        return subs
    has_all = "all" in subs
    subs = [s for s in subs if s != "all"]
    n = len(subs)
    head: list[str] = []
    if has_all:
        roll = random.Random(zlib.crc32(f"{account}:{int(poll_no)}".encode("utf-8"))).random()
        if roll < float(all_chance) or n == 0:
            head = ["all"]
    if k >= n:
        return head + subs
    order = sorted(subs)  # стабильная база: порядок в конфиге на выбор не влияет
    random.Random(zlib.crc32(account.encode("utf-8"))).shuffle(order)
    start = (int(poll_no) * k) % n
    return head + [order[(start + i) % n] for i in range(k)]
