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
    stale_note,
    standing_line,
)
from mmrbot.texts import HIDDEN_HINT, NO_PLAYERS, NOT_FOUND, STRATZ_OFF
from mmrbot.charts import _games_word as games_word, render_mmr_chart, series_stats
from mmrbot.heroes import find_hero
from mmrbot.opendota import OpenDota
from mmrbot.stats import period_since
from mmrbot.storage import Storage
from mmrbot.tracker import (
    REFRESH_COOLDOWN,
    finish_refresh,
    build_chat_comparison,
    build_hero_view,
    build_leaderboard,
    refresh_chat,
    build_match_view,
    build_period_leaderboard,
    build_player_heroes,
    build_player_roles,
    build_together,
    build_mmr_series,
    build_period_awards,
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


# Фоновая «вторая половина» обновления после ответа на команду: не больше одной задачи на чат.
_finish_running: set[tuple] = set()
_finish_tasks: set = set()


def _kick_finish(storage: Storage, od: OpenDota, chat_id: int) -> None:
    """Догнать всё, чего команда не ждала (детали матчей, средние/линии), уже после ответа."""
    if od is None or storage is None:
        return
    key = (storage.db_path, chat_id)
    if key in _finish_running:
        return
    _finish_running.add(key)

    async def run() -> None:
        try:
            await asyncio.to_thread(finish_refresh, storage, od, chat_id)
        except Exception:
            log.debug("Фоновое дообновление чата %s не удалось", chat_id, exc_info=True)
        finally:
            _finish_running.discard(key)

    task = asyncio.get_running_loop().create_task(run())
    _finish_tasks.add(task)  # держим ссылку, пока задача не завершится
    task.add_done_callback(_finish_tasks.discard)


async def gather_summaries(
    storage: Storage, od: OpenDota, chat_id: int, refresh: bool = True, stratz=None, complete: bool = False
):
    """Обновить игроков и собрать сводки.

    complete=False (команды): ждём матчи, ранг и позиции Stratz — один-два запроса OpenDota на игрока;
    детали матчей и средние/линии догоняются фоном сразу после ответа. True (ежедневная сводка): ждём всё.
    """
    async with _chat_lock(chat_id):
        now = int(time.time())  # момент берём уже под локом — кулдаун считается от актуального времени
        result = await asyncio.to_thread(build_leaderboard, storage, od, chat_id, now, refresh, stratz, not complete)
    if refresh and not complete:
        _kick_finish(storage, od, chat_id)
    return result


async def refresh_only(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> None:
    """Только обновить матчи игроков (под локом чата) — без сборки сводок, когда они не нужны."""
    async with _chat_lock(chat_id):
        await asyncio.to_thread(refresh_chat, storage, od, chat_id, int(time.time()), stratz, None, True)
    _kick_finish(storage, od, chat_id)


def _with_stale(storage: Storage, chat_id: int, text: str) -> str:
    """Дописать предупреждение, если кого-то не удалось обновить и показаны сохранённые данные."""
    note = stale_note(storage.list_players(chat_id), int(time.time()), REFRESH_COOLDOWN)
    return f"{text}\n\n{note}" if note else text


async def render_board(
    storage: Storage,
    od: OpenDota,
    chat_id: int,
    today_only: bool = False,
    refresh: bool = True,
    stratz=None,
    awards_period: str = "week",
) -> str:
    """Рейтинг + «Пульс пати» + отличия за awards_period (week — для /stats, day — для ежедневной сводки)."""
    summaries = await gather_summaries(storage, od, chat_id, refresh, stratz, complete=awards_period == "day")
    text = render_leaderboard(summaries, today_only=today_only)
    if not today_only:
        since = int(time.time()) - 7 * 86_400
        week_rows = await asyncio.to_thread(build_period_leaderboard, storage, chat_id, since)
        week_records = await asyncio.to_thread(build_records, storage, chat_id, since)
        day_rows = None
        if awards_period == "day":
            day_rows = await asyncio.to_thread(build_period_leaderboard, storage, chat_id, int(time.time()) - 86_400)
        text += "\n\n" + render_party_pulse(summaries, week_rows, week_records, day_rows)
        if len(summaries) >= 2:  # «отличия» — соревнование между игроками: с одним участником смысла нет
            day = awards_period == "day"
            awards_since = int(time.time()) - (86_400 if day else 7 * 86_400)
            awards = await asyncio.to_thread(build_period_awards, storage, chat_id, awards_since, 2 if day else 3)
            if not day:  # «Лидер недели» в «Пульсе» уже называет того, кто поднялся больше всех
                awards = [a for a in awards if a["key"] != "climb"]
            block = render_awards(awards, "за сутки" if day else "за неделю")
            if block:
                text += "\n\n" + block
    return _with_stale(storage, chat_id, text) if refresh else text


async def render_period_board(
    storage: Storage, od: OpenDota, chat_id: int, period: str, stratz=None
) -> str:
    await refresh_only(storage, od, chat_id, stratz)
    since = period_since(period, int(time.time()))
    rows = await asyncio.to_thread(build_period_leaderboard, storage, chat_id, since)
    return _with_stale(storage, chat_id, render_period_leaderboard(rows, period))


GRAPH_CACHE_TTL = 60  # сек: повторный график того же периода (переключение кнопок туда-обратно) — мгновенно
_graph_cache: dict[tuple, tuple[float, Optional[tuple[bytes, str]]]] = {}


async def render_graph_board(
    storage: Storage, od: OpenDota, chat_id: int, period: str, stratz=None, refresh: bool = True,
    by_games: bool = False,
) -> Optional[tuple[bytes, str]]:
    """PNG-график ±MMR за период и подпись; None — за период игр не было.

    refresh=False — без запроса в OpenDota (смена периода под уже показанным графиком: данные только что обновлены).
    """
    if refresh:  # для графика нужны только свежие матчи — сводки игроков не собираем (кулдаун внутри)
        await refresh_only(storage, od, chat_id, stratz)
    chat = storage.get_or_create_chat(chat_id)
    # В ключе — отпечаток данных: пришла новая игра — старая картинка не отдаётся (как и при смене шага/пояса).
    version = await asyncio.to_thread(storage.data_version, chat_id)
    cache_key = (storage.db_path, chat_id, period, chat.mmr_step, chat.tz, by_games, version)
    cached = _graph_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < GRAPH_CACHE_TTL:
        return cached[1]
    for key in [k for k, v in _graph_cache.items() if time.monotonic() - v[0] >= GRAPH_CACHE_TTL]:
        _graph_cache.pop(key, None)  # просроченные картинки не копим в памяти
    now = int(time.time())
    since = period_since(period, now)
    series = await asyncio.to_thread(build_mmr_series, storage, chat_id, since)
    if not series:
        _graph_cache[cache_key] = (time.monotonic(), None)
        return None
    label = {"day": "за сутки", "week": "за неделю", "month": "за месяц", "year": "за год",
             "all": "за всё время"}[period]
    png = await asyncio.to_thread(render_mmr_chart, series, f"Динамика MMR {label}", chat.tz, since, now, by_games)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (name, pts) in enumerate(sorted(series.items(), key=lambda kv: kv[1][-1][1], reverse=True)[:10]):  # лимит подписи фото — 1024
        games, wins, total = series_stats(pts)
        lines.append(f"{medals[i] if i < 3 else '▫️'} <b>{html.escape(name)}</b> {total:+d} · {games_word(games)} · {round(wins * 100 / games)}%")
    caption = f"📈 <b>Динамика MMR {label}</b> · <i>оценка: ±{chat.mmr_step} за игру</i>\n" + "\n".join(lines)
    if refresh:
        caption = _with_stale(storage, chat_id, caption)
    result = (png, caption)
    _graph_cache[cache_key] = (time.monotonic(), result)
    return result


async def render_records_board(storage: Storage, od: OpenDota, chat_id: int, period: str, stratz=None) -> str:
    await refresh_only(storage, od, chat_id, stratz)
    since = period_since(period, int(time.time()))
    data = await asyncio.to_thread(build_records, storage, chat_id, since)
    return _with_stale(storage, chat_id, render_records(data, period, storage.get_or_create_chat(chat_id).tz))


async def render_heroes_board(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> str:
    summaries = await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    return _with_stale(storage, chat_id, render_heroes(summaries))


async def render_together_board(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> str:
    # Сначала обновляем матчи всех игроков, затем считаем совместную статистику.
    await refresh_only(storage, od, chat_id, stratz)
    result = await asyncio.to_thread(build_together, storage, chat_id)
    return _with_stale(storage, chat_id, render_together(result))


async def render_player_board(storage: Storage, od: OpenDota, chat_id: int, name: str, stratz=None) -> Optional[str]:
    summaries = await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    comparison = build_chat_comparison(summaries)
    name_lower = name.strip().lower()
    for summary in summaries:
        if summary.display_name.lower() == name_lower or str(summary.account_id) == name.strip():
            standing = standing_line(comparison, summary.display_name)
            return _with_stale(storage, chat_id, render_player_card(summary, standing=standing))
    return None


async def render_compare_board(storage: Storage, od: OpenDota, chat_id: int, stratz=None) -> str:
    summaries = await gather_summaries(storage, od, chat_id, refresh=True, stratz=stratz)
    if not summaries:
        return NO_PLAYERS
    comparison = build_chat_comparison(summaries)
    return _with_stale(storage, chat_id, render_compare_table(comparison, summaries))


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

def _empty_players(storage: Storage, chat_id: int, name: Optional[str]) -> list[str]:
    """Имена игроков (одного или всех в чате), у которых в БД нет ни одного матча."""
    players = [storage.get_player(chat_id, name)] if name else storage.list_players(chat_id)
    return [p.display_name for p in players if p is not None and not storage.has_matches(p.id)]


async def render_player_heroes_board(
    storage: Storage, od: OpenDota, chat_id: int, name: str, period: str, stratz=None
) -> str:
    await refresh_only(storage, od, chat_id, stratz)
    since = period_since(period, int(time.time()))
    result = await asyncio.to_thread(build_player_heroes, storage, chat_id, name, since)
    if result is None:
        return NOT_FOUND
    player, rows = result
    text = render_player_heroes(player.display_name, period, rows)
    if not rows and not storage.has_matches(player.id):
        text += "\n" + HIDDEN_HINT
    roles = await asyncio.to_thread(build_player_roles, storage, chat_id, name, since)
    if roles is not None and roles[1]:
        text += "\n\n" + render_roles(player.display_name, roles[1], period)
    return text


async def render_roles_board(
    storage: Storage, od: OpenDota, chat_id: int, name: str, period: str = "all", stratz=None
) -> str:
    await refresh_only(storage, od, chat_id, stratz)
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
        await refresh_only(storage, od, chat_id, stratz)
        view = await asyncio.to_thread(build_match_view, storage, chat_id, name, None)
        if view is None:
            empty = _empty_players(storage, chat_id, name)
            who = html.escape(", ".join(empty)) if empty else "участников пати"  # уйдёт с parse_mode=HTML
            return f"Ранкед-матчей не видно ({who}). " + HIDDEN_HINT
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
        return render_full_match(full, tracked, focus, storage.get_or_create_chat(chat_id).tz)
    if cached is not None:
        return render_match_card(cached, storage.get_or_create_chat(chat_id).tz)
    if stratz is None:
        return STRATZ_OFF
    return f"Матч {match_id} не найден в Stratz (возможно, не ранкед или скрыт)."


async def render_hero_board(
    storage: Storage, od: OpenDota, chat_id: int, query: str, period: str, stratz=None
) -> str:
    hero_id = find_hero(query)
    if hero_id is None:
        return f"Герой «{query}» не найден. Пишите по-английски, например: /heroes Axe"
    await refresh_only(storage, od, chat_id, stratz)
    since = period_since(period, int(time.time()))
    entries = await asyncio.to_thread(build_hero_view, storage, chat_id, hero_id, since)
    return render_hero_detail(hero_id, period, entries)
