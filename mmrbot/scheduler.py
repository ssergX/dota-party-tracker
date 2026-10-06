"""Ежедневный дайджест: раз в час проверяем чаты и шлём тем, у кого настал их день.

Момент прогона снимается один раз (now_utc), поэтому долгий цикл не «сползает» по часу.
Идемпотентность на день — через last_digest_date: дайджест уходит один раз в локальные
сутки, а при простое, накрывшем нужный час, досылается при первом же прогоне после часа.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from mmrbot.opendota import OpenDota
from mmrbot.backup import backup_db
from mmrbot.formatting import (
    render_achievement_alert,
    render_game_alert,
    render_start_alert,
    render_steam_change,
    render_weekly,
)
from mmrbot.service import _chat_lock, render_board, split_message
from mmrbot.storage import Chat, Storage
from mmrbot.tags import sync_member_tags
from mmrbot.tracker import (
    backfill_opendota,
    backfill_stratz,
    build_weekly_report,
    detect_new_games,
    detect_presence,
    detect_steam_changes,
    refresh_heroes,
)

log = logging.getLogger(__name__)


def due_local_date(chat: Chat, now_utc: datetime) -> Optional[str]:
    """Вернуть ISO-дату локальных суток чата, если дайджест сейчас нужен, иначе None.

    Нужен, если по локальному времени час >= digest_hour и за эти локальные сутки
    дайджест ещё не отправляли.
    """
    try:
        tz = pytz.timezone(chat.tz)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")
    local = now_utc.astimezone(tz)
    today = local.date().isoformat()
    if chat.last_digest_date == today:
        return None
    if local.hour < chat.digest_hour:
        return None
    return today


def due_weekly_key(chat: Chat, now_utc: datetime) -> Optional[str]:
    """Ключ ISO-недели (ГГГГ-Wнн), если недельную сводку пора слать (понедельник после часа сводки)."""
    try:
        tz = pytz.timezone(chat.tz)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")
    local = now_utc.astimezone(tz)
    if local.weekday() != 0 or local.hour < chat.digest_hour:
        return None
    iso = local.isocalendar()
    key = f"{iso[0]}-W{iso[1]:02d}"
    return None if chat.last_weekly == key else key


async def send_digest(bot: Bot, storage: Storage, od: OpenDota, chat: Chat, due_date: str, stratz=None) -> None:
    """Собрать и отправить дайджест в один чат; отметить сутки отправленными.

    Если бота выгнали из чата/заблокировали (Forbidden / чат не найден), отмечаем сутки
    сделанными: иначе каждый час повторялось бы полное обновление игроков впустую.
    """
    try:
        text = await render_board(storage, od, chat.chat_id, today_only=False, refresh=True, stratz=stratz, awards_period="day")
        for chunk in split_message("📰 <b>Ежедневная сводка</b>\n\n" + text):
            await bot.send_message(chat.chat_id, chunk, parse_mode="HTML")
        storage.set_last_digest_date(chat.chat_id, due_date)
    except TelegramForbiddenError:
        log.warning("Бот потерял доступ к чату %s — дайджест пропущен на сегодня", chat.chat_id)
        storage.set_last_digest_date(chat.chat_id, due_date)
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            log.warning("Чат %s не найден — дайджест пропущен на сегодня", chat.chat_id)
            storage.set_last_digest_date(chat.chat_id, due_date)
        else:
            log.exception("Не удалось отправить дайджест в чат %s", chat.chat_id)
    except Exception:  # один битый чат не должен рушить остальные
        log.exception("Не удалось отправить дайджест в чат %s", chat.chat_id)


def setup_scheduler(
    bot: Bot, storage: Storage, od: OpenDota, stratz=None, backup_keep: int = 7, steam=None
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    async def hourly_digest() -> None:
        now_utc = datetime.now(timezone.utc)  # снимок момента прогона — один на все чаты
        for chat in storage.list_chats():
            due_date = due_local_date(chat, now_utc)
            if due_date is None:
                continue
            if not storage.list_players(chat.chat_id):
                continue
            await send_digest(bot, storage, od, chat, due_date, stratz)

    async def stratz_backfill() -> None:
        if stratz is None:
            return
        try:
            await asyncio.to_thread(backfill_stratz, storage, stratz)
        except Exception:
            log.exception("Фоновое дозаполнение Stratz не удалось")

    async def opendota_backfill() -> None:
        try:
            await asyncio.to_thread(backfill_opendota, storage, od)
        except Exception:
            log.exception("Фоновое обогащение матчей OpenDota не удалось")

    async def heroes_refresh() -> None:
        """Справочник героев: раз в сутки и вскоре после старта."""
        try:
            added = await asyncio.to_thread(refresh_heroes, od)
            if added:
                log.info("Справочник героев пополнен: +%d", added)
        except Exception:
            log.exception("Обновление справочника героев не удалось")

    async def steam_watch() -> None:
        """Оповещения о смене ника/аватарки Steam (профили берём из OpenDota)."""
        try:
            events = await asyncio.to_thread(detect_steam_changes, storage, od)
        except Exception:
            log.exception("Проверка смены Steam-профилей не удалась")
            return
        for event in events:
            text = render_steam_change(event["player"], event["changes"])
            avatar = event["profile"].get("avatarfull")
            try:
                if event["changes"].get("avatar") and avatar:
                    await bot.send_photo(event["chat_id"], avatar, caption=text, parse_mode="HTML")
                else:
                    await bot.send_message(event["chat_id"], text, parse_mode="HTML")
            except Exception:
                log.warning("Не удалось отправить оповещение Steam в чат %s", event["chat_id"], exc_info=True)

    async def tags_sync() -> None:
        """Теги участников с их MMR (чаты с /tags on); MMR берётся из кэша БД."""
        now = int(datetime.now(timezone.utc).timestamp())
        for chat in storage.list_chats():
            if not chat.tag_mmr:
                continue
            try:
                await sync_member_tags(bot, storage, chat.chat_id, now)
            except Exception:
                log.exception("Обновление тегов в чате %s не удалось", chat.chat_id)

    scheduler.add_job(tags_sync, "interval", minutes=30, misfire_grace_time=300, max_instances=1,
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120))
    scheduler.add_job(heroes_refresh, "cron", hour=5, minute=10, misfire_grace_time=3600)
    scheduler.add_job(heroes_refresh, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=45))

    # Каждые 30 минут сверяем ник/аватарку; первый прогон через минуту после старта (заполняет базу).
    scheduler.add_job(
        steam_watch, "interval", minutes=30, misfire_grace_time=300, max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    async def game_watch() -> None:
        """Оповещения о новых играх и достижениях (по матчам не старше нескольких часов)."""
        now = int(datetime.now(timezone.utc).timestamp())
        for chat in storage.list_chats():
            if not chat.notify_games or not storage.list_players(chat.chat_id):
                continue
            try:
                async with _chat_lock(chat.chat_id):
                    events = await asyncio.to_thread(detect_new_games, storage, od, chat, now, stratz)
            except Exception:
                log.exception("Проверка новых игр в чате %s не удалась", chat.chat_id)
                continue
            for event in events:
                text = render_game_alert(event) if event["kind"] == "match" else render_achievement_alert(event)
                try:
                    await bot.send_message(chat.chat_id, text, parse_mode="HTML")
                except Exception:
                    log.warning("Не удалось отправить оповещение в чат %s", chat.chat_id, exc_info=True)

    async def presence_watch() -> None:
        """Оповещения «зашёл в Dota 2» (Steam Web API); без ключа задача не регистрируется."""
        try:
            events = await asyncio.to_thread(detect_presence, storage, steam, int(datetime.now(timezone.utc).timestamp()))
        except Exception as exc:  # текст ошибки Steam-клиента ключа не содержит
            log.warning("Проверка статуса Steam не удалась: %s", exc)
            return
        for event in events:
            try:
                await bot.send_message(event["chat_id"], render_start_alert(event), parse_mode="HTML")
            except Exception:
                log.warning("Не удалось отправить оповещение о заходе в Dota в чат %s", event["chat_id"], exc_info=True)

    async def weekly_summary() -> None:
        now_utc = datetime.now(timezone.utc)
        for chat in storage.list_chats():
            key = due_weekly_key(chat, now_utc)
            if key is None or not chat.notify_weekly or not storage.list_players(chat.chat_id):
                continue
            try:
                report = await asyncio.to_thread(build_weekly_report, storage, chat.chat_id, int(now_utc.timestamp()))
                for chunk in split_message(render_weekly(report)):
                    await bot.send_message(chat.chat_id, chunk, parse_mode="HTML")
                storage.set_last_weekly(chat.chat_id, key)
            except (TelegramForbiddenError, TelegramBadRequest):
                log.warning("Недельная сводка в чат %s не доставлена — пропускаю неделю", chat.chat_id)
                storage.set_last_weekly(chat.chat_id, key)
            except Exception:
                log.exception("Недельная сводка в чат %s не удалась", chat.chat_id)

    async def daily_backup() -> None:
        try:
            await asyncio.to_thread(backup_db, storage.db_path, backup_keep)
        except Exception:
            log.exception("Бэкап БД не удался")

    # Новые игры: каждые 4 минуты (OpenDota-кулдаун внутри не даёт дёргать API чаще раза в 2 минуты на игрока;
    # без ключа и пока пати не играет — не чаще раза в 10 минут, см. tracker.GAME_IDLE_COOLDOWN).
    scheduler.add_job(game_watch, "interval", minutes=4, misfire_grace_time=120, max_instances=1,
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90))
    if steam is not None:
        scheduler.add_job(presence_watch, "interval", minutes=2, misfire_grace_time=120, max_instances=1,
                          next_run_time=datetime.now(timezone.utc) + timedelta(seconds=75))
    scheduler.add_job(weekly_summary, "cron", minute=5, misfire_grace_time=300)
    # Бэкап БД: раз в сутки ночью + один раз вскоре после старта (если за сегодня копии ещё нет).
    scheduler.add_job(daily_backup, "cron", hour=4, minute=30, misfire_grace_time=3600)
    scheduler.add_job(daily_backup, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=20))

    # Бэклог перф/бенчмарков (по 1 запросу на матч) разгребаем фоном, а не в /stats.
    scheduler.add_job(opendota_backfill, "interval", minutes=2, misfire_grace_time=120, max_instances=1)

    # Каждые 3 минуты дозаполняем позиции/IMP по истории (лимиты Stratz это выдерживают).
    scheduler.add_job(stratz_backfill, "interval", minutes=3, misfire_grace_time=120, max_instances=1)

    # Раз в час на :00; misfire_grace_time — переживаем короткие простои.
    scheduler.add_job(hourly_digest, "cron", minute=0, misfire_grace_time=300)
    return scheduler
