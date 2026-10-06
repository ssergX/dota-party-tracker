"""Состояние «игрок в Dota 2» — чистая логика переходов (без сети и БД)."""
from __future__ import annotations

from typing import Optional

MISS_LIMIT = 2  # столько опросов подряд без Dota — и сессия считается законченной (гасит дребезг статуса Steam)


def advance(
    since: Optional[int], misses: int, in_game: Optional[bool], now: int,
) -> tuple[Optional[int], int, Optional[str]]:
    """Один шаг опроса → (since, misses, событие).

    since — когда игрок зашёл в Dota (None — не в игре); событие: "start" | "end" | None.
    in_game=None — статус скрыт/неизвестен: состояние не трогаем.
    """
    if in_game is None:
        return since, misses, None
    if in_game:
        if since is None:
            return now, 0, "start"
        return since, 0, None
    if since is None:
        return None, 0, None
    misses += 1
    if misses >= MISS_LIMIT:
        return None, 0, "end"
    return since, misses, None
