"""Чистый разбор аргументов команд (без aiogram) — легко тестируется."""
from __future__ import annotations

from typing import Optional

MMR_MIN = 0
MMR_MAX = 20000


def _is_int(token: str) -> bool:
    try:
        int(token)
        return True
    except (TypeError, ValueError):
        return False


def _validate_mmr(mmr: int) -> int:
    if mmr < MMR_MIN or mmr > MMR_MAX:
        raise ValueError(f"MMR — число от {MMR_MIN} до {MMR_MAX}.")
    return mmr


def parse_add_args(args: str) -> tuple[str, Optional[str], Optional[int]]:
    """`/add <identifier> [Имя ...] [MMR]` → (identifier, name|None, mmr|None).

    Эвристика: первый токен — идентификатор; если последний токен — число, это MMR;
    всё между ними — имя (может быть из нескольких слов).
    """
    tokens = (args or "").split()
    if not tokens:
        raise ValueError("Формат: /add ссылка_или_ID [имя] [MMR]")

    identifier = tokens[0]
    rest = tokens[1:]

    mmr: Optional[int] = None
    if rest and _is_int(rest[-1]):
        mmr = _validate_mmr(int(rest[-1]))
        rest = rest[:-1]

    name = " ".join(rest) if rest else None
    return identifier, name, mmr


def parse_name_and_mmr(args: str) -> tuple[str, int]:
    """`/setmmr <Имя ...> <MMR>` → (name, mmr)."""
    tokens = (args or "").split()
    if len(tokens) < 2 or not _is_int(tokens[-1]):
        raise ValueError("Формат: /setmmr Вася 5300")
    mmr = _validate_mmr(int(tokens[-1]))
    name = " ".join(tokens[:-1])
    return name, mmr


def parse_mmr_value(text: str) -> int:
    """Ответ на подсказку кнопки «Задать MMR»: одно число в допустимом диапазоне."""
    token = (text or "").strip()
    if not _is_int(token):
        raise ValueError("Нужно одно число, например 5300.")
    return _validate_mmr(int(token))


def parse_step(args: str) -> int:
    """`/setstep <шаг>` → положительный int."""
    token = (args or "").strip()
    if not _is_int(token):
        raise ValueError("Формат: /setstep 25")
    step = int(token)
    if step <= 0 or step > 200:
        raise ValueError("Шаг — число от 1 до 200.")
    return step


def parse_hour(args: str) -> int:
    """`/settime <час>` → 0..23."""
    token = (args or "").strip()
    if not _is_int(token):
        raise ValueError("Формат: /settime 10 (час от 0 до 23)")
    hour = int(token)
    if hour < 0 or hour > 23:
        raise ValueError("Час — число от 0 до 23.")
    return hour


_PERIOD_WORDS = {
    "день": "day", "сутки": "day", "сегодня": "day", "day": "day", "today": "day",
    "неделя": "week", "неделю": "week", "week": "week",
    "месяц": "month", "month": "month",
    "год": "year", "year": "year",
    "всё": "all", "все": "all", "всё_время": "all", "all": "all",
}
_LAST_WORDS = {"последний", "последняя", "last", "latest"}


def _clean_name(tokens: list[str]) -> Optional[str]:
    name = " ".join(tokens).strip().lstrip("@").strip()
    return name or None


def parse_target_period(args: str) -> tuple[Optional[str], str]:
    """`[игрок|@тег] [период]` → (имя|None, 'day'|'week'|'month'|'all'). Период — в любом месте."""
    period = "all"
    rest = []
    for token in (args or "").split():
        key = token.lower()
        if key in _PERIOD_WORDS:
            period = _PERIOD_WORDS[key]
        else:
            rest.append(token)
    return _clean_name(rest), period


def parse_match_args(args: str) -> tuple[Optional[int], Optional[str]]:
    """`[match_id|последний] [игрок|@тег]` → (match_id|None, имя|None)."""
    match_id = None
    rest = []
    for token in (args or "").split():
        if token.lower() in _LAST_WORDS:
            continue
        if match_id is None and token.isascii() and token.isdigit() and len(token) >= 8:
            match_id = int(token)
        else:
            rest.append(token)
    return match_id, _clean_name(rest)


def parse_on_off(args: str) -> bool:
    """`/tags on|off` → True/False."""
    token = (args or "").strip().lower()
    if token in {"on", "вкл", "включить", "1"}:
        return True
    if token in {"off", "выкл", "выключить", "0"}:
        return False
    raise ValueError("Формат: /tags on или /tags off")
