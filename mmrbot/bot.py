"""aiogram-хендлеры команд бота."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InputMediaPhoto, Message

from mmrbot import commands as cmd
from mmrbot.formatting import render_achievements, render_player_list, render_settings, render_steam_profile, tz_label
from mmrbot.heroes import find_hero
from mmrbot.ids import resolve_account_id
from mmrbot.keyboards import STEPS, TIMEZONES, graph_buttons, main_menu, nav_menu, records_buttons, settings_menu, parse_callback, period_buttons, players_picker
from mmrbot.opendota import OpenDota
from mmrbot.service import (
    render_board,
    render_compare_board,
    render_hero_board,
    render_graph_board,
    render_records_board,
    render_heroes_board,
    render_match_board,
    render_period_board,
    render_player_board,
    render_player_heroes_board,
    render_roles_board,
    render_together_board,
    split_message,
)
from mmrbot.storage import Storage
from mmrbot.ranks import rank_label
from mmrbot.tracker import build_leaderboard, check_achievements, list_achievements, refresh_player

router = Router()

# Меню команд (всплывает по «/», особенно полезно в группах).
BOT_COMMANDS = [
    BotCommand(command="stats", description="🏆 Лидерборд + награды (/stats сегодня|неделя|месяц)"),
    BotCommand(command="compare", description="⚡ Кто сильнее в чате (сравнение)"),
    BotCommand(command="together", description="🤝 Совместные игры пати"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="heroes", description="Герои и позиции игрока: /heroes [ник|герой] [период]"),
    BotCommand(command="match", description="Разбор матча: /match [id] [ник|@тег]"),
    BotCommand(command="player", description="Карточка игрока: /player Имя"),
    BotCommand(command="records", description="🌟 Рекорды пати: /records день|неделя|месяц|год|всё"),
    BotCommand(command="graph", description="📈 График MMR: /graph неделя|месяц|всё"),
    BotCommand(command="achievements", description="🏅 Достижения и антирекорды"),
    BotCommand(command="steam", description="Steam-профиль: аватар и ник — /steam Имя"),
    BotCommand(command="add", description="Добавить игрока: /add ссылка Имя MMR"),
    BotCommand(command="list", description="Список игроков"),
    BotCommand(command="settings", description="⚙️ Настройки чата (шаг MMR, сводка, часовой пояс)"),
    BotCommand(command="setmmr", description="Установить MMR: /setmmr Имя 5400"),
    BotCommand(command="setstep", description="Шаг оценки MMR за игру"),
    BotCommand(command="settime", description="Время ежедневной сводки (МСК)"),
    BotCommand(command="remove", description="Удалить игрока: /remove Имя"),
    BotCommand(command="help", description="Справка"),
]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)

HELP_TEXT = (
    '📖 Справка · бот статистики Dota 2\n\n'
    'Бот ведёт историю ранкед-игр участников пати: количество игр, оценку изменения MMR, ранг, винрейт, KDA, героев и позиции.\n\n'
    '📡 Источники данных\n'
    '• OpenDota — основа: матчи, ранг, GPM, перф. Работает всегда.\n'
    '• Stratz — дополнение (нужен STRATZ_API_KEY на стороне бота): позиция (Pos 1–5), роль, линия и IMP. Без ключа бот работает, но раздел «Позиции» пуст, а IMP показывается как «—».\n'
    '• Данные доступны только при включённой в Dota опции «Открытые данные матчей».\n\n'
    '🧮 Что означают показатели\n'
    '• ≈ MMR — оценка: стартовое значение ± шаг за каждую ранкед-игру (точный MMR Dota 2 не отдаёт). Корректируется командой /setmmr.\n'
    '• 📊 IMP (Impact, Stratz) — вклад в исход матча относительно других игроков на том же герое и позиции. 0 — средний уровень, плюс — лучше среднего, минус — хуже. Саппорты обычно ниже керри, поэтому сравнивайте IMP в пределах одной позиции.\n'
    '• Перф — перцентиль игры относительно игроков на том же герое (OpenDota), 0–100.\n'
    '• KDA — (убийства + помощь) / смерти. GPM — золото в минуту.\n'
    '• 🟢🟡🔴 — винрейт: от 55% / 48–54% / ниже 48%.\n\n'
    '👥 Игроки и настройки\n'
    '/add <ссылка или ID> [Имя] [MMR] — добавить аккаунт\n'
    '   пример: /add dotabuff.com/players/123456 Вася 5400\n'
    '/list — список участников\n'
    '/remove <Имя> — удалить игрока\n'
    '/setmmr <Имя> <MMR> — установить или скорректировать MMR\n'
    '/setstep <шаг> — шаг оценки MMR за игру (по умолчанию 25)\n'
    '/settime <час> — время ежедневной сводки (МСК)\n\n'
    '🏆 Статистика\n'
    '/stats — рейтинг участников и отличия\n'
    '/stats сегодня — игры с 00:00 по времени чата\n'
    '/stats неделя · /stats месяц — итоги за период\n'
    '/compare — сравнение участников по показателям\n'
    '/together — статистика совместных игр\n'
    '/player <Имя> — карточка игрока (GPM, соло/группа, время суток)\n\n'
    '🦸 Герои, позиции, матчи\n'
    '/heroes — самые частые герои участников\n'
    '/heroes <ник|@тег> [день|неделя|месяц|всё] — герои и позиции игрока за период (позиции и IMP — Stratz)\n'
    '/heroes <герой> [период] — результаты участников на выбранном герое\n'
    '/match [id] [ник|@тег] — разбор матча (по умолчанию — последний)\n\n'
    '/steam <Имя> — аватарка и ник из Steam (бот сам сообщит, когда ник или аватарка сменятся)\n\n'
    '/records [день|неделя|месяц|год|всё] — рекорды пати: макс. GPM, убийства, урон и др. с героем и матчем\n'
    '/graph [день|неделя|месяц|всё] — график динамики MMR по игрокам\n'
    '/achievements [Имя] — достижения и антирекорды (бот сам сообщит о новых)\n\n'
    '⚙️ /settings — шаг MMR, время сводки, часовой пояс, оповещения Steam\n'
    '📋 /menu — главное меню с кнопками'
)


async def _delete(message) -> None:
    """Удалить сообщение бота (старше 48ч или без прав — молча пропускаем)."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def _progress(message: Message, text: str):
    """Временное «⏳ Формирование…» — удаляется, когда отчёт готов."""
    return await message.answer(text)


async def _send_chunks(message: Message, text: str, markup=None) -> None:
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, parse_mode="HTML", reply_markup=markup if i == len(chunks) - 1 else None)


async def _reply_board(message: Message, coro, status=None) -> None:
    """Общий помощник: собрать текст (с обработкой ошибок сети) и отправить чанками.

    status — временное сообщение «Формирование…» (удаляется); под отчётом кнопки «В меню»/«Закрыть».
    """
    try:
        text = await coro
    except Exception:
        logging.getLogger(__name__).exception("Ошибка сборки статистики для чата %s", message.chat.id)
        await _delete(status)
        await message.answer("⚠️ Не удалось получить данные OpenDota. Повторите запрос позднее.", reply_markup=nav_menu())
        return
    await _delete(status)
    if text is None:
        await message.answer("🔍 Игрок не найден. Список игроков: /list", reply_markup=nav_menu())
        return
    await _send_chunks(message, text, nav_menu())


@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("📋 Выберите раздел:", reply_markup=main_menu())


@router.message(Command("add"))
async def cmd_add(
    message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None
) -> None:
    try:
        identifier, name, mmr = cmd.parse_add_args(command.args or "")
        account_id = await asyncio.to_thread(resolve_account_id, identifier)
    except ValueError as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return

    storage.get_or_create_chat(message.chat.id)
    now = int(time.time())

    if not name:
        try:
            profile = await asyncio.to_thread(od.get_profile, account_id)
            name = profile.get("personaname") or f"id{account_id}"
        except Exception:
            name = f"id{account_id}"

    try:
        player = storage.add_player(message.chat.id, account_id, name, mmr, now, now)
    except ValueError as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return

    try:
        await asyncio.to_thread(refresh_player, storage, od, player, now, stratz)
    except Exception:
        pass  # первичная подгрузка не критична — досчитается в /stats

    try:  # история при добавлении — не «новые игры»: помечаем оповещённой и запоминаем достижения молча
        storage.mark_notified(player.id)
        check_achievements(storage, player, now)
    except Exception:
        logging.getLogger(__name__).warning("Не удалось инициализировать оповещения игрока", exc_info=True)

    mmr_note = f", стартовый MMR ≈ {mmr}" if mmr is not None else " (MMR не указан; задать можно командой /setmmr)"
    await message.answer(
        f"Игрок {name} (id {account_id}) добавлен{mmr_note}.\n"
        f"Учёт ранкед-игр ведётся с текущего момента. Рейтинг участников: /stats"
    )


async def _roster_text(storage: Storage, chat_id: int) -> str:
    """Список игроков из кэша БД (без запросов в сеть): текущий MMR считается по сохранённым матчам."""
    summaries = await asyncio.to_thread(build_leaderboard, storage, None, chat_id, int(time.time()), False)
    return render_player_list(summaries)


@router.message(Command("list"))
async def cmd_list(message: Message, storage: Storage) -> None:
    await message.answer(await _roster_text(storage, message.chat.id), parse_mode="HTML")


@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject, storage: Storage) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer("ℹ️ Формат команды: /remove Имя")
        return
    ok = storage.remove_player(message.chat.id, name)
    await message.answer("🗑️ Игрок удалён." if ok else "🔍 Игрок не найден. Список игроков: /list")


@router.message(Command("setmmr"))
async def cmd_setmmr(message: Message, command: CommandObject, storage: Storage) -> None:
    try:
        name, mmr = cmd.parse_name_and_mmr(command.args or "")
    except ValueError as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return
    player = storage.get_player(message.chat.id, name)
    if player is None:
        await message.answer("🔍 Игрок не найден. Список игроков: /list")
        return
    storage.set_player_anchor(player.id, mmr, int(time.time()))
    await message.answer(
        f"MMR игрока {player.display_name} установлен на ≈ {mmr}. Дальнейшая оценка ведётся от этого значения."
    )


@router.message(Command("setstep"))
async def cmd_setstep(message: Message, command: CommandObject, storage: Storage) -> None:
    try:
        step = cmd.parse_step(command.args or "")
    except ValueError as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return
    storage.set_chat_step(message.chat.id, step)
    await message.answer(f"⚙️ Шаг оценки MMR установлен: ±{step} за ранкед-игру.")


@router.message(Command("settime"))
async def cmd_settime(message: Message, command: CommandObject, storage: Storage) -> None:
    try:
        hour = cmd.parse_hour(command.args or "")
    except ValueError as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return
    storage.set_chat_digest_hour(message.chat.id, hour)
    tz = storage.get_or_create_chat(message.chat.id).tz
    await message.answer(f"⏰ Ежедневная сводка будет отправляться в {hour:02d}:00 ({tz_label(tz)}).")


NO_PLAYERS = "В данном чате нет игроков. Для добавления используйте: /add <ссылка или ID> Имя [MMR]"
NOT_FOUND_TEXT = "🔍 Игрок не найден. Список игроков: /list"


async def _has_players(message: Message, storage: Storage) -> bool:
    if storage.list_players(message.chat.id):
        return True
    await message.answer(NO_PLAYERS)
    return False


async def _ask_player(message: Message, storage: Storage, kind: str, prompt: str) -> None:
    await message.answer(prompt, reply_markup=players_picker(storage.list_players(message.chat.id), kind))


# --- действия (общие для команд и кнопок) -------------------------------

async def do_stats(message: Message, storage: Storage, od: OpenDota, stratz=None, today_only: bool = False) -> None:
    if not await _has_players(message, storage):
        return
    status = await _progress(message, "⏳ Формирование статистики за сегодня…" if today_only else "⏳ Формирование статистики…")
    await _reply_board(
        message, render_board(storage, od, message.chat.id, today_only=today_only, refresh=True, stratz=stratz), status
    )


async def do_period_stats(message: Message, storage: Storage, od: OpenDota, period: str, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    status = await _progress(message, "⏳ Формирование статистики за период…")
    await _reply_board(message, render_period_board(storage, od, message.chat.id, period, stratz), status)


async def do_together(message: Message, storage: Storage, od: OpenDota, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    status = await _progress(message, "⏳ Формирование статистики совместных игр…")
    await _reply_board(message, render_together_board(storage, od, message.chat.id, stratz), status)


async def do_compare(message: Message, storage: Storage, od: OpenDota, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    status = await _progress(message, "⏳ Формирование сравнения…")
    await _reply_board(message, render_compare_board(storage, od, message.chat.id, stratz), status)


async def do_heroes_board(message: Message, storage: Storage, od: OpenDota, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    status = await _progress(message, "⏳ Формирование статистики по героям…")
    await _reply_board(message, render_heroes_board(storage, od, message.chat.id, stratz), status)


async def _reply_with_period(message: Message, make_coro, prefix: str, storage: Storage, name: str,
                             period: str, edit: bool) -> None:
    """Борд игрока + ряд кнопок периода (edit=True — правим сообщение с нажатой кнопкой)."""
    player = storage.get_player(message.chat.id, name)
    if player is None:
        await message.answer(NOT_FOUND_TEXT)
        return
    status = None if edit else await _progress(message, "⏳ Формирование отчёта…")
    try:
        text = await make_coro()
    except Exception:
        logging.getLogger(__name__).exception("Ошибка сборки борда для чата %s", message.chat.id)
        await _delete(status)
        await message.answer("⚠️ Не удалось получить данные. Повторите запрос позднее.", reply_markup=nav_menu())
        return
    await _delete(status)
    markup = period_buttons(prefix, player.account_id, period)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass  # «message is not modified» и т.п. — шлём новым сообщением
    await _send_chunks(message, text, markup)


async def do_player_heroes(message: Message, storage: Storage, od: OpenDota, name: str, period: str,
                           stratz=None, edit: bool = False) -> None:
    await _reply_with_period(
        message, lambda: render_player_heroes_board(storage, od, message.chat.id, name, period, stratz),
        "hp", storage, name, period, edit,
    )


async def do_roles(message: Message, storage: Storage, od: OpenDota, name: str, period: str,
                   stratz=None, edit: bool = False) -> None:
    await _reply_with_period(
        message, lambda: render_roles_board(storage, od, message.chat.id, name, period, stratz),
        "rp", storage, name, period, edit,
    )


async def do_match(message: Message, storage: Storage, od: OpenDota, name, match_id, stratz=None) -> None:
    status = await _progress(message, "🔎 Поиск матча…")
    await _reply_board(message, render_match_board(storage, od, message.chat.id, name, match_id, stratz), status)


async def do_player_card(message: Message, storage: Storage, od: OpenDota, name: str, stratz=None) -> None:
    status = await _progress(message, f"⏳ Формирование карточки игрока {name}…")
    await _reply_board(message, render_player_board(storage, od, message.chat.id, name, stratz), status)


async def do_records(message: Message, storage: Storage, od: OpenDota, period: str, stratz=None,
                     edit: bool = False) -> None:
    """Рекорды пати за период; edit=True — правим сообщение с нажатой кнопкой периода."""
    status = None if edit else await _progress(message, "⏳ Собираю рекорды…")
    try:
        text = await render_records_board(storage, od, message.chat.id, period, stratz)
    except Exception:
        logging.getLogger(__name__).exception("Ошибка сборки рекордов для чата %s", message.chat.id)
        await _delete(status)
        if not edit:
            await message.answer("⚠️ Не удалось получить данные. Повторите запрос позднее.", reply_markup=nav_menu())
        return
    await _delete(status)
    markup = records_buttons(period)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as exc:
            if "not modified" not in str(exc):
                logging.getLogger(__name__).exception("Не удалось сменить период рекордов")
        return
    await _send_chunks(message, text, markup)


async def do_graph(message: Message, storage: Storage, od: OpenDota, period: str, stratz=None) -> None:
    """График ±MMR по игрокам за период (картинка + кнопки периодов)."""
    if not await _has_players(message, storage):
        return
    status = await _progress(message, "⏳ Рисую график…")
    try:
        result = await render_graph_board(storage, od, message.chat.id, period, stratz)
    except Exception:
        logging.getLogger(__name__).exception("Ошибка построения графика для чата %s", message.chat.id)
        await _delete(status)
        await message.answer("⚠️ Не удалось построить график. Повторите запрос позднее.", reply_markup=nav_menu())
        return
    await _delete(status)
    if result is None:
        await message.answer("💤 За выбранный период ранкед-игр не было.", reply_markup=graph_buttons(period))
        return
    png, caption = result
    await message.answer_photo(
        BufferedInputFile(png, filename="mmr.png"), caption=caption, parse_mode="HTML",
        reply_markup=graph_buttons(period),
    )


async def edit_graph(message: Message, storage: Storage, od: OpenDota, period: str, stratz=None) -> None:
    """Смена периода под графиком: подменяем картинку и подпись в том же сообщении."""
    try:
        result = await render_graph_board(storage, od, message.chat.id, period, stratz, refresh=False)
    except Exception:
        logging.getLogger(__name__).exception("Ошибка построения графика для чата %s", message.chat.id)
        return
    try:
        if result is None:
            await message.edit_caption(
                caption="💤 За выбранный период ранкед-игр не было.", reply_markup=graph_buttons(period)
            )
            return
        png, caption = result
        await message.edit_media(
            InputMediaPhoto(media=BufferedInputFile(png, filename="mmr.png"), caption=caption, parse_mode="HTML"),
            reply_markup=graph_buttons(period),
        )
    except Exception as exc:
        if "not modified" in str(exc):
            return  # тот же период — картинка не изменилась
        logging.getLogger(__name__).exception("Не удалось сменить период графика")


async def do_achievements(message: Message, storage: Storage, name) -> None:
    rows = await asyncio.to_thread(list_achievements, storage, message.chat.id, int(time.time()), name)
    if not rows:
        await message.answer(NOT_FOUND_TEXT, reply_markup=nav_menu())
        return
    for chunk in split_message(render_achievements(rows)):
        await message.answer(chunk, parse_mode="HTML", reply_markup=nav_menu())


async def do_steam(message: Message, storage: Storage, od: OpenDota, name: str) -> None:
    """Аватарка + текущий ник из профиля Steam (через OpenDota) одним сообщением."""
    player = storage.get_player(message.chat.id, name)
    if player is None:
        await message.answer(NOT_FOUND_TEXT, reply_markup=nav_menu())
        return
    status = await _progress(message, f"⏳ Загружаю Steam-профиль {player.display_name}…")
    try:
        profile = await asyncio.to_thread(od.get_profile, player.account_id)
    except Exception:
        logging.getLogger(__name__).exception("Ошибка загрузки Steam-профиля %s", player.account_id)
        await _delete(status)
        await message.answer("⚠️ Не удалось получить профиль. Повторите запрос позднее.", reply_markup=nav_menu())
        return
    await _delete(status)
    caption = render_steam_profile(
        player.display_name, player.account_id, profile,
        rank_label(profile.get("rank_tier"), profile.get("leaderboard_rank")),
    )
    avatar = profile.get("avatarfull")
    if avatar:
        try:
            await message.answer_photo(avatar, caption=caption, parse_mode="HTML", reply_markup=nav_menu())
            return
        except Exception:
            logging.getLogger(__name__).warning("Не удалось отправить аватар %s", avatar, exc_info=True)
    await message.answer(caption, parse_mode="HTML", reply_markup=nav_menu())


# --- команды ------------------------------------------------------------

@router.message(Command("stats"))
@router.message(Command("today"))
async def cmd_stats(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    """/stats — лидерборд; /stats сегодня|неделя|месяц — за период (/today — старый алиас)."""
    _, period = cmd.parse_target_period(command.args or "")
    if period in ("week", "month") and command.command != "today":
        await do_period_stats(message, storage, od, period, stratz)
        return
    today = period == "day" or command.command == "today"
    await do_stats(message, storage, od, stratz, today_only=today)


@router.message(Command("together"))
async def cmd_together(message: Message, storage: Storage, od: OpenDota, stratz=None) -> None:
    await do_together(message, storage, od, stratz)


@router.message(Command("compare"))
@router.message(Command("table"))
async def cmd_compare(message: Message, storage: Storage, od: OpenDota, stratz=None) -> None:
    await do_compare(message, storage, od, stratz)


@router.message(Command("heroes"))
async def cmd_heroes(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    name, period = cmd.parse_target_period(command.args or "")
    if name is None:
        await do_heroes_board(message, storage, od, stratz)
    elif storage.get_player(message.chat.id, name) is not None:
        await do_player_heroes(message, storage, od, name, period, stratz)  # герои + позиции игрока
    elif find_hero(name) is not None:
        status = await _progress(message, "⏳ Формирование статистики по герою…")
        await _reply_board(message, render_hero_board(storage, od, message.chat.id, name, period, stratz), status)
    else:
        await message.answer(f"🔍 Игрок или герой «{name}» не найден. Список игроков: /list; пример запроса по герою: /heroes Axe")


@router.message(Command("match"))
async def cmd_match(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    match_id, name = cmd.parse_match_args(command.args or "")
    if match_id is None and not await _has_players(message, storage):
        return
    await do_match(message, storage, od, name, match_id, stratz)


@router.message(Command("roles"))
async def cmd_roles(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    name, period = cmd.parse_target_period(command.args or "")
    if name is None:
        await _ask_player(message, storage, "roles", "Выберите игрока для просмотра позиций:")
    else:
        await do_roles(message, storage, od, name, period, stratz)


@router.message(Command("records"))
async def cmd_records(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    if not await _has_players(message, storage):
        return
    _, period = cmd.parse_target_period(command.args or "")
    await do_records(message, storage, od, period if (command.args or "").strip() else "week", stratz)


@router.message(Command("graph"))
async def cmd_graph(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    _, period = cmd.parse_target_period(command.args or "")
    await do_graph(message, storage, od, period if (command.args or "").strip() else "week", stratz)


@router.message(Command("achievements"))
async def cmd_achievements(message: Message, command: CommandObject, storage: Storage) -> None:
    name = (command.args or "").strip().lstrip("@").strip() or None
    await do_achievements(message, storage, name)


@router.message(Command("steam"))
async def cmd_steam(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    name = (command.args or "").strip().lstrip("@").strip()
    if not name:
        if await _has_players(message, storage):
            await _ask_player(message, storage, "steam", "Выберите игрока для просмотра Steam-профиля:")
        return
    await do_steam(message, storage, od, name)


@router.message(Command("player"))
async def cmd_player(message: Message, command: CommandObject, storage: Storage, od: OpenDota, stratz=None) -> None:
    name = (command.args or "").strip().lstrip("@").strip()
    if not name:
        if await _has_players(message, storage):
            await _ask_player(message, storage, "player", "Выберите игрока для просмотра карточки:")
        return
    await do_player_card(message, storage, od, name, stratz)


# --- кнопки -------------------------------------------------------------

async def _on_settings(message: Message, storage: Storage, args: list[str]) -> None:
    """Кнопки настроек (`s:<что>:<значение>`): меняем значение и правим сообщение на месте."""
    chat_id = message.chat.id
    chat = storage.get_or_create_chat(chat_id)
    what = args[0] if args else ""
    value = args[1] if len(args) > 1 else ""
    try:
        if what == "step" and int(value) in STEPS:
            storage.set_chat_step(chat_id, int(value))
        elif what == "hour" and value in {"-1", "1"}:
            storage.set_chat_digest_hour(chat_id, (chat.digest_hour + int(value)) % 24)
        elif what == "tz" and 0 <= int(value) < len(TIMEZONES):
            storage.set_chat_tz(chat_id, TIMEZONES[int(value)][1])
        elif what == "steam":
            storage.set_chat_notify_steam(chat_id, not chat.notify_steam)
        elif what == "games":
            storage.set_chat_notify_games(chat_id, not chat.notify_games)
        elif what == "weekly":
            storage.set_chat_notify_weekly(chat_id, not chat.notify_weekly)
        elif what != "noop":
            return
    except ValueError:
        return
    chat = storage.get_or_create_chat(chat_id)
    try:
        await message.edit_text(render_settings(chat), parse_mode="HTML", reply_markup=settings_menu(chat))
    except Exception:
        pass  # «message is not modified» — значение не изменилось


@router.message(Command("settings"))
async def cmd_settings(message: Message, storage: Storage) -> None:
    chat = storage.get_or_create_chat(message.chat.id)
    await message.answer(render_settings(chat), parse_mode="HTML", reply_markup=settings_menu(chat))


@router.callback_query(lambda c: bool(c.data) and c.data.split(":")[0] in {"m", "pp", "hp", "rp", "x", "s", "g", "r"})
async def on_callback(query: CallbackQuery, storage: Storage, od: OpenDota, stratz=None) -> None:
    await query.answer()  # убрать «часики» на кнопке
    message = query.message
    if message is None:
        return
    kind, args = parse_callback(query.data)

    if kind == "x":
        await _delete(message)
        return

    if kind == "s":
        await _on_settings(message, storage, args)
        return

    if kind == "r":
        period = args[0] if args else "week"
        if period in {"day", "week", "month", "year", "all"}:
            await do_records(message, storage, od, period, stratz, edit=True)  # меняем период на месте
        return

    if kind == "g":
        period = args[0] if args else "week"
        if period in {"day", "week", "month", "all"}:
            if getattr(message, "photo", None):
                await edit_graph(message, storage, od, period, stratz)  # график меняется на месте
            else:
                await _delete(message)
                await do_graph(message, storage, od, period, stratz)
        return

    # переход в другой раздел: старое сообщение (меню/выбор игрока/прошлый отчёт) убираем, чтобы не засорять чат;
    # смена периода (hp/rp) правит сообщение на месте
    if kind in {"m", "pp"}:
        await _delete(message)

    if kind == "m":
        action = args[0] if args else "menu"
        if action == "menu":
            await message.answer("📋 Выберите раздел:", reply_markup=main_menu())
        elif action == "records":
            if await _has_players(message, storage):
                await do_records(message, storage, od, "week", stratz)
        elif action == "graph":
            if await _has_players(message, storage):
                await do_graph(message, storage, od, "week", stratz)
        elif action == "achv":
            if await _has_players(message, storage):
                await do_achievements(message, storage, None)
        elif action == "settings":
            chat = storage.get_or_create_chat(message.chat.id)
            await message.answer(render_settings(chat), parse_mode="HTML", reply_markup=settings_menu(chat))
        elif action == "help":
            await message.answer(HELP_TEXT, reply_markup=nav_menu())
        elif action == "list":
            await message.answer(
                await _roster_text(storage, message.chat.id), parse_mode="HTML", reply_markup=nav_menu()
            )
        elif action == "stats":
            await do_stats(message, storage, od, stratz)
        elif action == "today":
            await do_stats(message, storage, od, stratz, today_only=True)
        elif action in {"week", "month"}:
            await do_period_stats(message, storage, od, action, stratz)
        elif action == "compare":
            await do_compare(message, storage, od, stratz)
        elif action == "together":
            await do_together(message, storage, od, stratz)
        elif action == "match":
            await do_match(message, storage, od, None, None, stratz)
        elif action in {"heroes", "roles", "player", "steam"}:
            if await _has_players(message, storage):
                label = {"heroes": "Выберите игрока для просмотра героев", "roles": "Выберите игрока для просмотра позиций", "player": "Выберите игрока для просмотра карточки", "steam": "Выберите игрока для просмотра Steam-профиля"}[action]
                await _ask_player(message, storage, action, f"{label}:")
        return

    if kind == "pp" and len(args) == 2:
        pick, account = args
        if pick == "heroes":
            await do_player_heroes(message, storage, od, account, "all", stratz)
        elif pick == "roles":
            await do_roles(message, storage, od, account, "all", stratz)
        elif pick == "player":
            await do_player_card(message, storage, od, account, stratz)
        elif pick == "steam":
            await do_steam(message, storage, od, account)
        return

    if kind in {"hp", "rp"} and len(args) == 2:
        account, period = args
        if period not in {"day", "week", "month", "all"}:
            return
        action = do_player_heroes if kind == "hp" else do_roles
        await action(message, storage, od, account, period, stratz, edit=True)
