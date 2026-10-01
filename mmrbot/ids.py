"""Разбор идентификатора игрока Dota 2 в 32-битный account_id.

Принимаем:
- ссылки Dotabuff/OpenDota/Stratz вида .../players/<id> (id уже 32-битный account_id);
- ссылки Steam .../profiles/<steamid64> (конвертируем в account_id);
- голое число: >= SteamID64-базы — это SteamID64, иначе — готовый account_id;
- кастомные ссылки Steam /id/<vanity> разрешает resolve_account_id через публичный XML профиля
  (без Steam API-ключа); сам parse_account_id остаётся чистым и на vanity даёт понятную ошибку.
"""
from __future__ import annotations

import re

STEAMID64_BASE = 76561197960265728


def _to_account_id(number: int) -> int:
    """SteamID64 -> account_id; готовый account_id оставляем как есть."""
    if number > STEAMID64_BASE:
        return number - STEAMID64_BASE
    return number


def parse_account_id(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Пустой идентификатор. Дай ссылку Dotabuff/OpenDota или числовой ID.")

    # Steam vanity-ссылка — без Steam API-ключа не разрешить.
    if re.search(r"steamcommunity\.com/id/", raw, flags=re.IGNORECASE):
        raise ValueError(
            "Не могу определить ID по кастомной Steam-ссылке (/id/...). "
            "Дай ссылку Dotabuff/OpenDota (.../players/<id>) или числовой ID."
        )

    # .../players/<id> (Dotabuff, OpenDota, Stratz) — id уже account_id.
    match = re.search(r"/players/(\d+)", raw)
    if match:
        return _to_account_id(int(match.group(1)))

    # .../profiles/<steamid64> (Steam).
    match = re.search(r"/profiles/(\d+)", raw)
    if match:
        return _to_account_id(int(match.group(1)))

    # Голое число.
    if re.fullmatch(r"\d+", raw):
        return _to_account_id(int(raw))

    raise ValueError(
        "Не понял идентификатор. Пришли ссылку Dotabuff/OpenDota (.../players/<id>) "
        "или числовой account_id / SteamID64."
    )


_VANITY_RE = re.compile(r"steamcommunity\.com/id/([^/?#\s]+)", re.IGNORECASE)
_STEAMID64_RE = re.compile(r"<steamID64>(\d+)</steamID64>")


def extract_vanity(text: str):
    """Имя из ссылки steamcommunity.com/id/<имя> (со схемой или без) → str | None."""
    match = _VANITY_RE.search(text or "")
    return match.group(1) if match else None


def resolve_account_id(text: str, session=None, timeout: int = 15) -> int:
    """Как parse_account_id, но ещё разрешает кастомные Steam-ссылки /id/<имя> через сеть."""
    vanity = extract_vanity(text)
    if vanity is None:
        return parse_account_id(text)
    if session is None:
        import requests
        session = requests
    match = None
    for attempt in range(3):  # Steam иногда рвёт соединение — пробуем ещё
        try:
            resp = session.get(f"https://steamcommunity.com/id/{vanity}/?xml=1", timeout=timeout)
        except Exception:
            continue
        if resp.status_code == 200:
            match = _STEAMID64_RE.search(resp.text)
            break  # профиль не найден — это ответ Steam, повторять незачем
    else:
        raise ValueError("Не смог обратиться к Steam, чтобы определить ID. Дай ссылку Dotabuff/OpenDota или число.")
    if not match:
        raise ValueError(
            f"Не нашёл Steam-профиль «{vanity}» (или он скрыт). Дай ссылку Dotabuff/OpenDota или числовой ID."
        )
    return _to_account_id(int(match.group(1)))
