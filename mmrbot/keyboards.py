"""Inline-клавиатуры бота (чистые билдеры, без обработчиков).

Схема callback_data (до 64 байт): `m:<действие>` — главное меню, `m:c:<категория>` — его подменю; `pp:<вид>:<account_id>` —
выбор игрока; `x:close` — удалить сообщение; `r:<период>` — рекорды; `hp:<account_id>:<период>` / `rp:<account_id>:<период>` — герои/позиции с периодом.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PERIODS = [("day", "День"), ("week", "Неделя"), ("month", "Месяц"), ("all", "Всё")]

# Главное меню — категории (`m:c:<ключ>`) и две «быстрые» кнопки; внутри категории — действия `m:<действие>`.
CATEGORIES = {
    "stats": ("🏆 Статистика", [
        [("🏆 Рейтинг", "stats"), ("📅 Сегодня", "today")],
        [("🗓️ За неделю", "week"), ("📆 За месяц", "month")],
        [("⚖️ Сравнение", "compare"), ("🤝 Совместные игры", "together")],
    ]),
    "players": ("🎮 Игроки и матчи", [
        [("🪪 Карточка игрока", "player"), ("🎭 Steam-профиль", "steam")],
        [("🦸 Герои и позиции", "heroes"), ("🔎 Статистика героя", "hero")],
        [("🎮 Матч", "match")],
    ]),
    "records": ("🌟 Рекорды и графики", [
        [("🌟 Рекорды", "records"), ("📈 График MMR", "graph")],
        [("🏅 Достижения", "achv")],
    ]),
    "party": ("👥 Пати", [
        [("👥 Игроки", "list"), ("➕ Добавить", "add")],
        [("✏️ Задать MMR", "setmmr"), ("🗑️ Удалить", "remove")],
        [("🙋 Это я", "me"), ("🏷️ Теги с MMR", "tags")],
    ]),
}
_MAIN = [
    [(CATEGORIES["stats"][0], "c:stats"), (CATEGORIES["players"][0], "c:players")],
    [(CATEGORIES["records"][0], "c:records"), (CATEGORIES["party"][0], "c:party")],
    [("⚙️ Настройки", "settings"), ("📖 Справка", "help")],
]
_CLOSE = ("✖️ Закрыть", "x:close")


STEPS = [10, 20, 25, 30, 50]
TIMEZONES = [
    ("Москва", "Europe/Moscow"), ("Киев", "Europe/Kiev"), ("Минск", "Europe/Minsk"), ("Алматы", "Asia/Almaty"),
    ("Самара", "Europe/Samara"), ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Новосибирск", "Asia/Novosibirsk"), ("Владивосток", "Asia/Vladivostok"),
]


def settings_menu(chat) -> InlineKeyboardMarkup:
    """Настройки чата: шаг MMR, час сводки, часовой пояс, оповещения Steam (`s:<что>:<значение>`)."""
    def btn(text, data):
        return InlineKeyboardButton(text=text, callback_data=data)

    rows = [
        [btn(f"• ±{n}" if n == chat.mmr_step else f"±{n}", f"s:step:{n}") for n in STEPS],
        [btn("◀️ −1 ч", "s:hour:-1"), btn(f"⏰ {chat.digest_hour:02d}:00", "s:noop"), btn("+1 ч ▶️", "s:hour:1")],
    ]
    tz_buttons = [
        btn(f"• {label}" if name == chat.tz else label, f"s:tz:{i}") for i, (label, name) in enumerate(TIMEZONES)
    ]
    rows += [tz_buttons[i:i + 4] for i in range(0, len(tz_buttons), 4)]
    rows.append([btn(f"🔔 Steam-профиль: {'вкл' if chat.notify_steam else 'выкл'}", "s:steam")])
    rows.append([btn(f"🎮 Конец матча и достижения: {'вкл' if chat.notify_games else 'выкл'}", "s:games")])
    rows.append([btn(f"📅 Недельная сводка: {'вкл' if chat.notify_weekly else 'выкл'}", "s:weekly")])
    rows.append([btn(f"🏷️ Теги с MMR: {'вкл' if chat.tag_mmr else 'выкл'}", "s:tags")])
    rows.append(nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


PERIODS_RECORDS = [("day", "День"), ("week", "Неделя"), ("month", "Месяц"), ("year", "Год"), ("all", "Всё")]
GRAPH_PERIODS = PERIODS_RECORDS  # у графика те же периоды, что у рекордов (включая год)


def records_buttons(current: str) -> InlineKeyboardMarkup:
    """Периоды рекордов (текущий отмечен «•») + навигация; `r:<период>`."""
    row = [
        InlineKeyboardButton(text=f"• {label}" if key == current else label, callback_data=f"r:{key}")
        for key, label in PERIODS_RECORDS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, nav_row()])


def graph_buttons(current: str, by_games: bool = False) -> InlineKeyboardMarkup:
    """Периоды графика (текущий отмечен «•») + переключатель оси X (время/игры) + навигация."""
    suffix = ":n" if by_games else ""
    row = [
        InlineKeyboardButton(text=f"• {label}" if key == current else label, callback_data=f"g:{key}{suffix}")
        for key, label in GRAPH_PERIODS
    ]
    toggle = InlineKeyboardButton(
        text="🕒 По времени" if by_games else "🔢 По играм",
        callback_data=f"g:{current}" if by_games else f"g:{current}:n",
    )
    return InlineKeyboardMarkup(inline_keyboard=[row, [toggle], nav_row()])


def nav_row() -> list[InlineKeyboardButton]:
    """Навигация под отчётом: назад в меню (старое сообщение удаляется) и закрыть."""
    return [
        InlineKeyboardButton(text="⬅️ В меню", callback_data="m:menu"),
        InlineKeyboardButton(text=_CLOSE[0], callback_data=_CLOSE[1]),
    ]


def nav_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[nav_row()])


def main_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t, callback_data=f"m:{a}") for t, a in row] for row in _MAIN]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_title(key: str) -> str:
    return CATEGORIES[key][0]


def category_menu(key: str) -> InlineKeyboardMarkup:
    """Подменю категории: её действия + «В меню»/«Закрыть»."""
    rows = [[InlineKeyboardButton(text=t, callback_data=f"m:{a}") for t, a in row] for row in CATEGORIES[key][1]]
    rows.append(nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def players_picker(players, kind: str, per_row: int = 2, extra=None) -> InlineKeyboardMarkup:
    """Кнопки с именами игроков → `pp:<kind>:<account_id>`; extra — доп. ряды; внизу «В меню»."""
    buttons = [
        InlineKeyboardButton(text=p.display_name, callback_data=f"pp:{kind}:{p.account_id}")
        for p in players
    ]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    rows += extra or []
    rows.append(nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_remove(account_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления игрока: `pp:rmyes:<account_id>` / отмена → меню."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑️ Да, удалить", callback_data=f"pp:rmyes:{account_id}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="m:menu"),
    ]])


def period_buttons(prefix: str, account_id: int, current: str) -> InlineKeyboardMarkup:
    """Ряд периодов (текущий отмечен «•») + «К игрокам»/«В меню»."""
    row = [
        InlineKeyboardButton(
            text=f"• {label}" if key == current else label,
            callback_data=f"{prefix}:{account_id}:{key}",
        )
        for key, label in PERIODS
    ]
    rows = [row]
    other = {"hp": ("rp", "🎯 Позиции"), "rp": ("hp", "🦸 Герои")}.get(prefix)
    if other:
        rows.append([InlineKeyboardButton(text=other[1], callback_data=f"{other[0]}:{account_id}:{current}")])
    rows.append(nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


STATS_TABS = [("stats", "🏆 Рейтинг"), ("today", "📅 Сегодня"), ("week", "🗓️ Неделя"), ("month", "📆 Месяц")]


def stats_tabs(current: str) -> InlineKeyboardMarkup:
    """Переключатель отчётов под рейтингом (текущий отмечен «•») + навигация; `m:<действие>`."""
    row = [
        InlineKeyboardButton(text=f"• {label}" if key == current else label, callback_data=f"m:{key}")
        for key, label in STATS_TABS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, nav_row()])


def player_actions(account_id: int) -> InlineKeyboardMarkup:
    """Под карточкой игрока: герои, позиции, Steam, достижения (`pp:<вид>:<account_id>`)."""
    def btn(text, kind):
        return InlineKeyboardButton(text=text, callback_data=f"pp:{kind}:{account_id}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🦸 Герои", "heroes"), btn("🎯 Позиции", "roles")],
        [btn("🎭 Steam", "steam"), btn("🏅 Достижения", "achv")],
        nav_row(),
    ])


def list_actions() -> InlineKeyboardMarkup:
    """Под списком игроков: управление составом."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="m:add"),
         InlineKeyboardButton(text="✏️ Задать MMR", callback_data="m:setmmr")],
        [InlineKeyboardButton(text="🗑️ Удалить", callback_data="m:remove")],
        nav_row(),
    ])


def parse_callback(data: str) -> tuple[str, list[str]]:
    parts = (data or "").split(":")
    return parts[0], [p for p in parts[1:]]
