"""Отличия участников за период (чистые функции, без сети и БД).

compute_period_awards([(имя, матчи за период)], step) → список {key, emoji, title, player, detail}.
Считаем только по матчам выбранного периода (сутки/неделя), а не по всей истории: иначе «отличия»
в ежедневной сводке не отражали бы сам день. Отличие имеет смысл только как сравнение: нужны минимум
двое игроков с данными по метрике и различающиеся значения (при равенстве награду не выдаём).
"""
from __future__ import annotations

from typing import Callable, Optional

from mmrbot.stats import estimate_mmr_delta, is_win, longest_win_streak


def _games(n: int) -> str:
    """«1 игра», «2 игры», «21 игра», «11 игр»."""
    n10, n100 = n % 10, n % 100
    word = "игра" if n10 == 1 and n100 != 11 else "игры" if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else "игр"
    return f"{n} {word}"


def _mean(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _longest_loss_streak(matches: list[dict]) -> int:
    longest = current = 0
    for match in matches:
        if is_win(match["player_slot"], match["radiant_win"]):
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _kda(match: dict) -> Optional[float]:
    kills, deaths, assists = match.get("kills") or 0, match.get("deaths") or 0, match.get("assists") or 0
    if kills + assists < 10:  # без порога «0 смертей, 2 помощи» стало бы лучшей игрой
        return None
    return (kills + assists) / max(deaths, 1)


def compute_period_awards(
    named_matches: list[tuple[str, list[dict]]], step: int = 25, min_games: int = 3
) -> list[dict]:
    """Награды за период. min_games — порог игр у игрока для метрик-средних (винрейт, GPM, урон…)."""
    players = {name: sorted(ms, key=lambda m: m["start_time"]) for name, ms in named_matches if ms}
    awards: list[dict] = []

    def add(key: str, emoji: str, title: str, values: dict[str, float], detail: Callable[[str], str],
            lowest: bool = False, floor: float = 0) -> None:
        if len(values) < 2:  # сравнивать не с кем
            return
        pick = min if lowest else max
        best = pick(values.values())
        if best == (max if lowest else min)(values.values()):  # у всех одинаково — отличия нет
            return
        if not lowest and best < floor:  # порог значимости (например, серия хотя бы из 2 игр)
            return
        winners = [n for n, v in values.items() if v == best]
        if len(winners) > 1:  # делёж первого места — не выдаём (иначе награда «случайная»)
            return
        awards.append({"key": key, "emoji": emoji, "title": title, "player": winners[0], "detail": detail(winners[0])})

    played = {n: ms for n, ms in players.items() if len(ms) >= 1}
    regular = {n: ms for n, ms in played.items() if len(ms) >= min_games}

    deltas = {
        n: estimate_mmr_delta(sum(is_win(m["player_slot"], m["radiant_win"]) for m in ms),
                              sum(not is_win(m["player_slot"], m["radiant_win"]) for m in ms), step)
        for n, ms in played.items()
    }
    top = deltas
    add("climb", "🚀", "Больше всех поднялся", top, lambda n: f"{deltas[n]:+d} MMR")
    if awards and awards[-1]["key"] == "climb" and deltas[awards[-1]["player"]] <= 0:
        awards.pop()  # «поднялся» с нулём или минусом — не отличие
    add("drop", "📉", "Больше всех просел", top, lambda n: f"{deltas[n]:+d} MMR", lowest=True)
    if awards and awards[-1]["key"] == "drop" and deltas[awards[-1]["player"]] >= 0:
        awards.pop()

    add("games", "🕹️", "Больше всех играл", {n: len(ms) for n, ms in played.items()},
        lambda n: _games(len(played[n])))

    winrates = {n: sum(is_win(m["player_slot"], m["radiant_win"]) for m in ms) / len(ms) for n, ms in regular.items()}
    add("winrate", "👑", "Лучший винрейт", winrates,
        lambda n: f"{winrates[n] * 100:.0f}% за {_games(len(regular[n]))}")

    def averages(field: str) -> dict[str, float]:
        result = {}
        for n, ms in regular.items():
            mean = _mean([m.get(field) for m in ms])
            if mean is not None:
                result[n] = mean
        return result

    perf = averages("perf_score")
    add("perf", "⭐", "Лучший перф", perf, lambda n: f"{perf[n] * 100:.0f}/100")
    gpm = averages("gpm")
    add("gpm", "💰", "Наибольший GPM", gpm, lambda n: f"{gpm[n]:.0f} GPM в среднем")
    damage = averages("hero_damage")
    add("damage", "💥", "Наибольший урон по героям", damage, lambda n: f"{damage[n] / 1000:.1f}k урона/игра")
    assists = averages("assists")
    add("assists", "🤝", "Больше всего ассистов", assists, lambda n: f"{assists[n]:.1f} ассистов/игра")

    best_games: dict[str, tuple[float, dict]] = {}
    for n, ms in played.items():
        scored = [(k, m) for m in ms if (k := _kda(m)) is not None]
        if scored:
            best_games[n] = max(scored, key=lambda x: x[0])
    add("best_game", "🎯", "Лучшая игра", {n: v[0] for n, v in best_games.items()},
        lambda n: "{}/{}/{} (KDA {:.1f})".format(
            best_games[n][1].get("kills") or 0, best_games[n][1].get("deaths") or 0,
            best_games[n][1].get("assists") or 0, best_games[n][0]))

    streaks = {n: longest_win_streak(ms) for n, ms in played.items()}
    add("win_streak", "🔥", "Лучшая серия побед", streaks, lambda n: f"{streaks[n]} подряд", floor=2)

    deaths = averages("deaths")
    add("deaths", "💀", "Больше всего смертей", deaths, lambda n: f"{deaths[n]:.1f} смертей/игра")

    loss = {n: _longest_loss_streak(ms) for n, ms in played.items()}
    add("loss_streak", "🧊", "Серия поражений", loss, lambda n: f"{loss[n]} подряд", floor=3)
    return awards
