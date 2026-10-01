"""Асинхронные помощники, общие для хендлеров и планировщика.

Сетевые/CPU-операции (build_leaderboard дергает синхронный OpenDota-клиент)
выносятся в поток через asyncio.to_thread, чтобы не блокировать event loop aiogram.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
import weakref
from typing import Optional

from mmrbot.formatting import (
    render_awards,
    render_party_pulse,
    render_compare_table,
    render_full_match,
    render_hero_detail,
    render_heroes,
    render_leaderboard,
    render_match_card,
    render_player_card,
    render_period_leaderboard,
    render_player_heroes,
    render_records,
    render_roles,
    render_together,
    standing_line,
)
from mmrbot.charts import render_mmr_chart
from mmrbot.heroes import find_hero
from mmrbot.opendota import OpenDota
from mmrbot.stats import period_since
from mmrbot.storage import Storage
from mmrbot.tracker import (
    build_chat_comparison,
    build_hero_view,
    build_leaderboard,
    build_match_view,
    build_period_leaderboard,
    build_player_heroes,
    build_player_roles,
    build_together,
    build_mmr_series,
    build_records,
)

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


# Блокировки по чату (на каждый event loop): одновременные команды в одном чате не обновляют
# игроков дважды — второй вызов ждёт первый и видит свежий кулдаун вместо повторных запросов.
_chat_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _chat_lock(chat_id: int) -> asyncio.Lock:
    per_loop = _chat_locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(chat_id, asyncio.Lock())


async def gather_summaries(
    storage: Storage, od: OpenDota, chat_id: int, refresh: bool = True, stratz=None
):
    async with _chat_lock(chat_id):
        now = int(time.time())  # момент берём уже под локом — кулдаун считается от актуального времени
        return await asyncio.to_thread(build_leaderboard, storage, od, chat_id, now, refresh, stratz)


async def render_board(
    storage: Storage,
    od: OpenDota,
    chat_id: int,
    today_only: bool = False,
    refresh: bool = True,
    stratz=None,
) -> str:
    summaries = await gather_summaries(storage, od, chat_id, refresh, stratz)
    text = render_leaderboard(summaries, today_only=today_only)
    if not today_only:
        since = int(time.time()) - 7 * 86_400
        week_rows = await asyncio.to_thread(build_period_leaderboard, storage, chat_id, since)
        week_records = await asyncio.to_thread(build_records, storage, chat_id, since)
        text += "\n\n" + render_party_pulse(summaries, week_rows, week_records)
        if len(summaries) >= 2:  # «отличия» — соревнование между игроками: с одним участником смысла нет
            awards = render_awards(summaries)
            if awards:
                text += "\n\n" + awards
    return text


async def render_period_board(
    storage: Storage, od: OpenDota, chat_id: int, period: str, stratz=None
) -> str:
    await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    since = period_since(period, int(time.time()))
    rows = await asyncio.to_thread(build_period_leaderboard, storage, chat_id, since)
    return render_period_leaderboard(rows, period)


GRAPH_CACHE_TTL = 60  # сек: повторный график того же периода (переключение кнопок туда-обратно) — мгновенно
_graph_cache: dict[tuple[str, int, str], tuple[float, Optional[tuple[bytes, str]]]] = {}


async def render_graph_board(
    storage: Storage, od: OpenDota, chat_id: int, period: str, stratz=None, refresh: bool = True
) -> Optional[tuple[bytes, str]]:
    """PNG-график ±MMR за период и подпись; None — за период игр не было.

    refresh=False — без запроса в OpenDota (смена периода под уже показанным графиком: данные только что обновлены).
    """
    cached = _graph_cache.get((storage.db_path, chat_id, period))
    if cached and time.monotonic() - cached[0] < GRAPH_CACHE_TTL:
        return cached[1]
    if refresh:
        await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    now = int(time.time())
    since = period_since(period, now)
    series = await asyncio.to_thread(build_mmr_series, storage, chat_id, since)
    if not series:
        _graph_cache[(storage.db_path, chat_id, period)] = (time.monotonic(), None)
        return None
    chat = storage.get_or_create_chat(chat_id)
    label = {"day": "за сутки", "week": "за неделю", "month": "за месяц", "year": "за год",
             "all": "за всё время"}[period]
    png = await asyncio.to_thread(render_mmr_chart, series, f"Динамика MMR {label}", chat.tz, since, now)
    result = (png, f"📈 <b>Динамика MMR {label}</b> · <i>оценка: ±шаг за игру</i>")
    _graph_cache[(storage.db_path, chat_id, period)] = (time.monotonic(), result)
    return result


async def render_records_board(storage: Storage, od: OpenDota, chat_id: int, period: str, stratz=None) -> str:
    await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    since = period_since(period, int(time.time()))
    data = await asyncio.to_thread(build_records, storage, chat_id, since)
    return render_records(data, period)


async def render_heroes_board(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> str:
    summaries = await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    return render_heroes(summaries)


async def render_together_board(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> str:
    # Сначала обновляем матчи всех игроков, затем считаем совместную статистику.
    await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    result = await asyncio.to_thread(build_together, storage, chat_id)
    return render_together(result)


async def render_player_board(storage: Storage, od: OpenDota, chat_id: int, name: str, stratz=None) -> Optional[str]:
    summaries = await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    comparison = build_chat_comparison(summaries)
    name_lower = name.strip().lower()
    for summary in summaries:
        if summary.display_name.lower() == name_lower or str(summary.account_id) == name.strip():
            standing = standing_line(comparison, summary.display_name)
            return render_player_card(summary, standing=standing)
    return None


async def render_compare_board(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> str:
    summaries = await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    if not summaries:
        return "В данном чате нет игроков. Для добавления используйте: /add «ссылка или ID» Имя [MMR]"
    comparison = build_chat_comparison(summaries)
    return render_compare_table(comparison, summaries)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Разбить длинное сообщение по границам блоков (двойной перевод строки).

    Блок, который сам по себе длиннее лимита, режется жёстко на куски по `limit`,
    чтобы ни один кусок не превысил лимит Telegram.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        # candidate не помещается: сначала сбрасываем накопленное.
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
        else:
            # Один блок длиннее лимита — режем жёстко.
            for i in range(0, len(block), limit):
                piece = block[i : i + limit]
                if len(piece) == limit:
                    chunks.append(piece)
                else:
                    current = piece  # хвост копим дальше
    if current:
        chunks.append(current)
    return chunks


# --- герои / позиции / матч (данные из кэша БД после обновления) ---------

NOT_FOUND = "Игрок не найден. Список игроков: /list"

HIDDEN_HINT = (
    "Матчи не найдены ни в OpenDota, ни в Stratz. Как правило, причина в отключённой в Dota 2 опции "
    "«Настройки → Социальные → Выставлять публичные данные матчей». Необходимо включить её и сыграть матч: "
    "после этого данные станут доступны (история до включения опции сервисам не видна)."
)


def _empty_players(storage: Storage, chat_id: int, name: Optional[str]) -> list[str]:
    """Имена игроков (одного или всех в чате), у которых в БД нет ни одного матча."""
    players = [storage.get_player(chat_id, name)] if name else storage.list_players(chat_id)
    return [p.display_name for p in players if p is not None and not storage.get_matches(p.id)]


async def render_player_heroes_board(
    storage: Storage, od: OpenDota, chat_id: int, name: str, period: str, stratz=None
) -> str:
    await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    since = period_since(period, int(time.time()))
    result = await asyncio.to_thread(build_player_heroes, storage, chat_id, name, since)
    if result is None:
        return NOT_FOUND
    player, rows = result
    text = render_player_heroes(player.display_name, period, rows)
    if not rows and not storage.get_matches(player.id):
        text += "\n" + HIDDEN_HINT
    roles = await asyncio.to_thread(build_player_roles, storage, chat_id, name, since)
    if roles is not None and roles[1]:
        text += "\n\n" + render_roles(player.display_name, roles[1], period)
    return text


async def render_roles_board(
    storage: Storage, od: OpenDota, chat_id: int, name: str, period: str = "all", stratz=None
) -> str:
    await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    since = period_since(period, int(time.time()))
    result = await asyncio.to_thread(build_player_roles, storage, chat_id, name, since)
    if result is None:
        return NOT_FOUND
    player, rows = result
    return render_roles(player.display_name, rows, period)


async def render_match_board(
    storage: Storage, od: OpenDota, chat_id: int, name: Optional[str], match_id: Optional[int], stratz=None
) -> str:
    """Карточка матча. С match_id — любой матч (не обязательно игроков пати), через Stratz.

    Без match_id — последний матч игрока (или самого свежего в чате). Если Stratz недоступен,
    для своих игроков показываем карточку из кэша БД.
    """
    tracked = {p.account_id: p.display_name for p in storage.list_players(chat_id)}
    focus = None
    cached = None
    if match_id is None:
        await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
        view = await asyncio.to_thread(build_match_view, storage, chat_id, name, None)
        if view is None:
            empty = _empty_players(storage, chat_id, name)
            who = html.escape(", ".join(empty)) if empty else "участников пати"  # уйдёт с parse_mode=HTML
            return f"Ранкед-матчи не найдены ({who}). " + HIDDEN_HINT
        cached = view
        match_id = view["match"]["match_id"]
        focus = view["player"].account_id
    elif name:
        target = storage.get_player(chat_id, name)
        if target is None:
            return NOT_FOUND
        focus = target.account_id

    full = None
    if stratz is not None:
        try:
            full = await asyncio.to_thread(stratz.get_match, match_id)
        except Exception:
            log.warning("Stratz: не удалось получить матч %s", match_id, exc_info=True)
    if full is not None:
        if focus is None:
            focus = next((p["account_id"] for p in full["players"] if p["account_id"] in tracked), None)
        return render_full_match(full, tracked, focus)
    if cached is not None:
        return render_match_card(cached)
    if stratz is None:
        return "Для разбора произвольного матча требуется STRATZ_API_KEY."
    return f"Матч {match_id} в Stratz не найден (возможно, он не ранкед либо скрыт). Повторите запрос позднее."


async def render_hero_board(
    storage: Storage, od: OpenDota, chat_id: int, query: str, period: str, stratz=None
) -> str:
    hero_id = find_hero(query)
    if hero_id is None:
        return f"Герой «{query}» не найден. Попробуйте английское название, например: /heroes Axe"
    await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    since = period_since(period, int(time.time()))
    entries = await asyncio.to_thread(build_hero_view, storage, chat_id, hero_id, since)
    return render_hero_detail(hero_id, period, entries)
