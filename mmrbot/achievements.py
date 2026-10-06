"""Постоянные достижения и антирекорды игрока (чистые функции, без сети и БД).

evaluate(matches) → какие достижения игрок заработал за всю сохранённую историю. Хранение и
оповещения о новых — в storage/tracker: тут только правила.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mmrbot.heroes import hero_name
from mmrbot.stats import is_win


@dataclass(frozen=True)
class Achievement:
    code: str
    emoji: str
    title: str
    anti: bool = False  # антирекорд (позорная «награда»)


CATALOG: dict[str, Achievement] = {a.code: a for a in [
    Achievement("win_streak_5", "🔥", "5 побед подряд"),
    Achievement("win_streak_10", "🔥", "10 побед подряд"),
    Achievement("win_streak_15", "🌋", "15 побед подряд"),
    Achievement("lose_streak_5", "💧", "5 поражений подряд", anti=True),
    Achievement("lose_streak_10", "🌊", "10 поражений подряд", anti=True),
    Achievement("games_50", "🎮", "50 ранкед-игр"),
    Achievement("games_100", "🎮", "100 ранкед-игр"),
    Achievement("games_250", "🎮", "250 ранкед-игр"),
    Achievement("games_500", "🏅", "500 ранкед-игр"),
    Achievement("games_1000", "🏆", "1000 ранкед-игр"),
    Achievement("hero_50", "🦸", "50 игр на одном герое"),
    Achievement("hero_100", "🦸", "100 игр на одном герое"),
    Achievement("hero_250", "🦸", "250 игр на одном герое"),
    Achievement("hero_500", "👑", "500 игр на одном герое"),
    Achievement("hero_1000", "👑", "1000 игр на одном герое"),
    Achievement("kills_20", "⚔️", "20+ убийств за игру"),
    Achievement("deathless", "🛡️", "Бессмертный: 10+ убийств+помощи и 0 смертей"),
    Achievement("deaths_20", "💀", "20+ смертей за игру", anti=True),
    Achievement("marathon", "⏳", "Марафон: игра дольше 60 минут"),
]}

_STREAKS = {"win_streak_": [5, 10, 15], "lose_streak_": [5, 10]}
_GAMES = [50, 100, 250, 500, 1000]
_HERO_GAMES = [50, 100, 250, 500, 1000]


def _longest(matches: list[dict], want_win: bool) -> int:
    longest = current = 0
    for m in matches:
        if is_win(m["player_slot"], m["radiant_win"]) == want_win:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def evaluate(matches: list[dict]) -> dict[str, Optional[str]]:
    """code → деталь (герой/число) для всех достижений, выполненных на этих матчах."""
    ordered = sorted(matches, key=lambda m: m["start_time"])
    earned: dict[str, Optional[str]] = {}

    win_len, lose_len = _longest(ordered, True), _longest(ordered, False)
    for need in _STREAKS["win_streak_"]:
        if win_len >= need:
            earned[f"win_streak_{need}"] = str(win_len)
    for need in _STREAKS["lose_streak_"]:
        if lose_len >= need:
            earned[f"lose_streak_{need}"] = str(lose_len)

    for need in _GAMES:
        if len(ordered) >= need:
            earned[f"games_{need}"] = str(len(ordered))

    by_hero: dict[int, int] = {}
    for m in ordered:
        if m.get("hero_id") is not None:
            by_hero[m["hero_id"]] = by_hero.get(m["hero_id"], 0) + 1
    if by_hero:
        hero_id, count = max(by_hero.items(), key=lambda kv: kv[1])
        for need in _HERO_GAMES:
            if count >= need:
                earned[f"hero_{need}"] = f"{hero_name(hero_id)} · {count}"

    for m in ordered:
        kills, deaths, assists = m.get("kills") or 0, m.get("deaths") or 0, m.get("assists") or 0
        hero = hero_name(m.get("hero_id"))
        if kills >= 20 and "kills_20" not in earned:
            earned["kills_20"] = f"{hero} {kills}/{deaths}/{assists}"
        if deaths == 0 and kills + assists >= 10 and "deathless" not in earned:
            earned["deathless"] = f"{hero} {kills}/{deaths}/{assists}"
        if deaths >= 20 and "deaths_20" not in earned:
            earned["deaths_20"] = f"{hero} {kills}/{deaths}/{assists}"
        if (m.get("duration") or 0) >= 3600 and "marathon" not in earned:
            earned["marathon"] = f"{hero} · {(m['duration']) // 60} мин"
    return earned


def _when(match: dict) -> int:
    """Момент, когда игра закончилась (для даты достижения); без длительности — начало."""
    return match["start_time"] + (match.get("duration") or 0)


def evaluate_timed(matches: list[dict]) -> dict[str, tuple[int, Optional[str]]]:
    """code → (когда заработано, деталь): дата — реальный матч, на котором порог был достигнут.

    Раньше датой считался день, когда бот заметил достижение: у всей старой истории стояла дата
    добавления игрока в чат.
    """
    ordered = sorted(matches, key=lambda m: m["start_time"])
    details = evaluate(ordered)
    times: dict[str, int] = {}

    for prefix, want_win in (("win_streak_", True), ("lose_streak_", False)):
        current = 0
        for m in ordered:
            current = current + 1 if is_win(m["player_slot"], m["radiant_win"]) == want_win else 0
            for need in _STREAKS[prefix]:
                if current >= need:
                    times.setdefault(f"{prefix}{need}", _when(m))

    for need in _GAMES:
        if len(ordered) >= need:
            times[f"games_{need}"] = _when(ordered[need - 1])

    by_hero: dict[int, list[dict]] = {}
    for m in ordered:
        if m.get("hero_id") is not None:
            by_hero.setdefault(m["hero_id"], []).append(m)
    if by_hero:
        hero_matches = max(by_hero.values(), key=len)
        for need in _HERO_GAMES:
            if len(hero_matches) >= need:
                times[f"hero_{need}"] = _when(hero_matches[need - 1])

    single = {
        "kills_20": lambda m: (m.get("kills") or 0) >= 20,
        "deathless": lambda m: (m.get("deaths") or 0) == 0 and (m.get("kills") or 0) + (m.get("assists") or 0) >= 10,
        "deaths_20": lambda m: (m.get("deaths") or 0) >= 20,
        "marathon": lambda m: (m.get("duration") or 0) >= 3600,
    }
    for code, test in single.items():
        hit = next((m for m in ordered if test(m)), None)
        if hit is not None:
            times[code] = _when(hit)

    return {code: (times.get(code, ordered[-1]["start_time"] if ordered else 0), detail) for code, detail in details.items()}
