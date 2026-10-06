"""Теги участников чата с MMR (Bot API 9.5: setChatMemberTag).

Тег — 0–16 символов без эмодзи; ставится только обычным участникам (админам и владельцу — нельзя),
боту нужно право администратора «управлять тегами».
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram.exceptions import TelegramAPIError

from mmrbot.tracker import build_leaderboard

log = logging.getLogger(__name__)


def format_tag(mmr: Optional[int]) -> Optional[str]:
    """Текст тега по MMR; None — MMR неизвестен, тег не трогаем."""
    if mmr is None:
        return None
    return f"{mmr} MMR"


def auto_link_user(storage, chat_id: int, user):
    """Привязать аккаунт к игроку, чей ник совпал с username/именем в Telegram (без учёта регистра).

    Не трогает уже привязанный аккаунт и уже занятого игрока; вернёт игрока или None.
    """
    if user is None or getattr(user, "is_bot", False):
        return None
    players = storage.list_players(chat_id)
    if any(p.tg_user_id == user.id for p in players):
        return None
    names = {n.strip().lstrip("@").lower() for n in (
        getattr(user, "username", None), getattr(user, "full_name", None), getattr(user, "first_name", None),
    ) if n}
    for player in players:
        if player.tg_user_id is None and player.display_name.strip().lower() in names:
            storage.link_user(chat_id, player.id, user.id)
            return player
    return None


def link_adder(storage, chat_id: int, player, user) -> bool:
    """Привязать автора `/add` к добавленному игроку, если у автора в чате ещё нет своего игрока."""
    if user is None or getattr(user, "is_bot", False):
        return False
    if any(p.tg_user_id == user.id for p in storage.list_players(chat_id)):
        return False
    storage.link_user(chat_id, player.id, user.id)
    return True


async def sync_member_tags(bot, storage, chat_id: int, now: int) -> int:
    """Привести теги привязанных игроков чата к их текущему MMR; вернуть число обновлённых тегов.

    Неизменившийся тег повторно не отправляем. Отказ Telegram по одному участнику (админ, нет прав,
    вышел из чата) не мешает остальным.
    """
    linked = {p.account_id: p for p in storage.list_players(chat_id) if p.tg_user_id}
    if not linked:
        return 0
    summaries = await asyncio.to_thread(build_leaderboard, storage, None, chat_id, now, False)
    updated = 0
    for summary in summaries:
        player = linked.get(summary.account_id)
        tag = format_tag(summary.current_mmr)
        if player is None or tag is None or tag == player.last_tag:
            continue
        try:
            await bot.set_chat_member_tag(chat_id=chat_id, user_id=player.tg_user_id, tag=tag)
        except TelegramAPIError as exc:
            log.warning("Тег для %s в чате %s не поставлен: %s", player.display_name, chat_id, exc)
            continue
        storage.set_player_tag(player.id, tag)
        updated += 1
    return updated


async def clear_member_tag(bot, chat_id: int, user_id: int) -> None:
    """Снять тег (при отвязке); ошибки Telegram глотаем — тег уже мог исчезнуть."""
    try:
        await bot.set_chat_member_tag(chat_id=chat_id, user_id=user_id, tag=None)
    except TelegramAPIError as exc:
        log.warning("Тег пользователя %s в чате %s не снят: %s", user_id, chat_id, exc)
