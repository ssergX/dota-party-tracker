"""Чистые функции статистики: победа/поражение, ранкед-фильтр, агрегаты, оценка MMR.

Никакого I/O — всё легко юнит-тестится. Окна по времени (с старта / за сутки /
с якоря) применяет вызывающий код (tracker) через выборки из хранилища.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytz

RANKED_LOBBY_TYPE = 7


def is_win(player_slot: int, radiant_win: bool) -> bool:
    """player_slot < 128 — игрок за Radiant. Победа = сторона игрока победила."""
    is_radiant = player_slot < 128
    return is_radiant == bool(radiant_win)


def is_ranked_lobby(lobby_type: Optional[int]) -> bool:
    return lobby_type == RANKED_LOBBY_TYPE


@dataclass
class Aggregate:
    games: int
    wins: int
    losses: int
    sum_kills: int
    sum_deaths: int
    sum_assists: int

    @property
    def winrate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def avg_kills(self) -> float:
        return self.sum_kills / self.games if self.games else 0.0

    @property
    def avg_deaths(self) -> float:
        return self.sum_deaths / self.games if self.games else 0.0

    @property
    def avg_assists(self) -> float:
        return self.sum_assists / self.games if self.games else 0.0

    @property
    def kda_ratio(self) -> float:
        """(K+A)/max(D,1) по сумме — стандартный агрегатный KDA."""
        if self.games == 0:
            return 0.0
        return (self.sum_kills + self.sum_assists) / max(self.sum_deaths, 1)


def aggregate(matches: list[dict]) -> Aggregate:
    """Свести список матчей в агрегат. Каждый матч: player_slot, radiant_win, kills, deaths, assists."""
    wins = 0
    sum_k = sum_d = sum_a = 0
    for match in matches:
        if is_win(match["player_slot"], match["radiant_win"]):
            wins += 1
        sum_k += match.get("kills", 0) or 0
        sum_d += match.get("deaths", 0) or 0
        sum_a += match.get("assists", 0) or 0
    games = len(matches)
    return Aggregate(
        games=games,
        wins=wins,
        losses=games - wins,
        sum_kills=sum_k,
        sum_deaths=sum_d,
        sum_assists=sum_a,
    )


def estimate_mmr_delta(wins: int, losses: int, step: int) -> int:
    """Оценка изменения MMR: (победы - поражения) * шаг."""
    return (wins - losses) * step


def current_streak(matches: list[dict]) -> tuple[str, int]:
    """Текущая серия по хвосту (матчи должны быть по возрастанию времени).

    Возвращает ('W'|'L', длина) или ('', 0), если матчей нет.
    """
    if not matches:
        return ("", 0)
    last_win = is_win(matches[-1]["player_slot"], matches[-1]["radiant_win"])
    length = 0
    for match in reversed(matches):
        if is_win(match["player_slot"], match["radiant_win"]) == last_win:
            length += 1
        else:
            break
    return ("W" if last_win else "L", length)


def top_heroes(matches: list[dict], k: int = 3) -> list[dict]:
    """Топ-k героев по числу игр (тай-брейк: винрейт, затем hero_id)."""
    by_hero: dict[int, list[int]] = {}  # hero_id -> [games, wins]
    for match in matches:
        hero_id = match.get("hero_id")
        if not hero_id:
            continue
        stat = by_hero.setdefault(hero_id, [0, 0])
        stat[0] += 1
        if is_win(match["player_slot"], match["radiant_win"]):
            stat[1] += 1
    result = [
        {"hero_id": hid, "games": games, "wins": wins, "winrate": wins / games}
        for hid, (games, wins) in by_hero.items()
    ]
    result.sort(key=lambda h: (h["games"], h["winrate"], -h["hero_id"]), reverse=True)
    return result[:k]


def winrate_by_hour(matches: list[dict], tz_name: str) -> dict[int, tuple[int, int]]:
    """Разбивка по локальному часу старта: hour -> (игр, побед)."""
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")
    by_hour: dict[int, list[int]] = {}
    for match in matches:
        start_time = match.get("start_time")
        if start_time is None:
            continue
        hour = datetime.fromtimestamp(start_time, tz=timezone.utc).astimezone(tz).hour
        stat = by_hour.setdefault(hour, [0, 0])
        stat[0] += 1
        if is_win(match["player_slot"], match["radiant_win"]):
            stat[1] += 1
    return {hour: (games, wins) for hour, (games, wins) in by_hour.items()}


def solo_party_split(matches: list[dict]) -> dict[str, tuple[int, int]]:
    """Соло (party_size<=1 или отсутствует) vs пати: bucket -> (игр, побед)."""
    buckets = {"solo": [0, 0], "party": [0, 0]}
    for match in matches:
        party_size = match.get("party_size") or 1
        key = "party" if party_size > 1 else "solo"
        buckets[key][0] += 1
        if is_win(match["player_slot"], match["radiant_win"]):
            buckets[key][1] += 1
    return {key: (games, wins) for key, (games, wins) in buckets.items()}


# Benchmark-метрики, где выше = лучше (deaths исключаем — высокий процент это плохо).
_POSITIVE_BENCHMARKS = {
    "gold_per_min",
    "xp_per_min",
    "kills_per_min",
    "assists_per_min",
    "last_hits_per_min",
    "hero_damage_per_min",
    "hero_healing_per_min",
    "tower_damage_per_min",
    "stuns_per_min",
}


def perf_score(benchmarks: dict) -> Optional[float]:
    """Role-normalized перформанс: среднее перцентилей «полезных» benchmark-метрик (0..1).

    benchmarks — перцентили игрока против других на ТОМ ЖЕ герое → метрика честна к роли
    (саппорт сравнивается с саппортами, кор — с корами). Домашний аналог STRATZ IMP.
    """
    pcts = [
        value
        for metric, value in (benchmarks or {}).items()
        if metric in _POSITIVE_BENCHMARKS and value is not None
    ]
    if not pcts:
        return None
    return sum(pcts) / len(pcts)


def aggregate_skill(benchmarks_list: list[dict]) -> dict:
    """Средний перцентиль по каждой benchmark-метрике за набор матчей.

    Даёт объективный профиль скилла: где стоишь относительно других на тех же героях
    (50% = средний игрок мира).
    """
    sums: dict = {}
    counts: dict = {}
    for benchmarks in benchmarks_list:
        for metric, pct in (benchmarks or {}).items():
            if pct is None:
                continue
            sums[metric] = sums.get(metric, 0.0) + pct
            counts[metric] = counts.get(metric, 0) + 1
    return {metric: sums[metric] / counts[metric] for metric in sums}


def infer_role_style(avg_last_hits: Optional[float], avg_hero_healing: Optional[float]) -> str:
    """Грубая эвристика стиля/роли по сырым средним (не перцентилям)."""
    if avg_last_hits is None:
        return ""
    if (avg_hero_healing or 0) > 4000:
        return "саппорт (хилер)"
    if avg_last_hits >= 180:
        return "кор (фарм)"
    if avg_last_hits < 90:
        return "саппорт / роумер"
    return "универсал"


def recent_form(matches: list[dict], n: int = 5) -> list[bool]:
    """Последние n матчей как список исходов (True=победа), в хронологическом порядке."""
    tail = matches[-n:]
    return [is_win(m["player_slot"], m["radiant_win"]) for m in tail]


def best_game(matches: list[dict]) -> Optional[dict]:
    """Матч с максимальным KDA. Возвращает kills/deaths/assists/hero_id/kda или None."""
    best = None
    best_kda = -1.0
    for match in matches:
        kills = match.get("kills", 0) or 0
        deaths = match.get("deaths", 0) or 0
        assists = match.get("assists", 0) or 0
        kda = (kills + assists) / max(deaths, 1)
        if kda > best_kda:
            best_kda = kda
            best = {
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "hero_id": match.get("hero_id"),
                "kda": kda,
            }
    return best


def longest_win_streak(matches: list[dict]) -> int:
    """Самая длинная серия побед подряд за весь набор матчей."""
    longest = current = 0
    for match in matches:
        if is_win(match["player_slot"], match["radiant_win"]):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def wins_losses_split(matches: list[dict]) -> dict:
    """Средние KDA/смерти отдельно в победах и поражениях (тащишь или разваливаешься)."""
    def _agg(subset: list[dict]) -> Optional[dict]:
        if not subset:
            return None
        deaths = sum((m.get("deaths", 0) or 0) for m in subset) / len(subset)
        kda = sum(
            ((m.get("kills", 0) or 0) + (m.get("assists", 0) or 0)) / max(m.get("deaths", 0) or 0, 1)
            for m in subset
        ) / len(subset)
        return {"games": len(subset), "avg_deaths": deaths, "avg_kda": kda}

    wins = [m for m in matches if is_win(m["player_slot"], m["radiant_win"])]
    losses = [m for m in matches if not is_win(m["player_slot"], m["radiant_win"])]
    return {"win": _agg(wins), "loss": _agg(losses)}


def hero_pool(matches: list[dict]) -> int:
    """Число разных героев (широта пула)."""
    return len({m["hero_id"] for m in matches if m.get("hero_id")})


def duration_stats(matches: list[dict]) -> dict[str, float]:
    """Средняя и максимальная длительность (в минутах) по полю duration (секунды)."""
    durations = [m["duration"] for m in matches if m.get("duration")]
    if not durations:
        return {"avg_minutes": 0, "max_minutes": 0}
    return {
        "avg_minutes": sum(durations) / len(durations) / 60,
        "max_minutes": max(durations) / 60,
    }


PERIOD_SECONDS = {"day": 86_400, "week": 7 * 86_400, "month": 30 * 86_400, "year": 365 * 86_400}


def period_since(period: str, now: int) -> Optional[int]:
    """'day'|'week'|'month'|'year' → граница start_time; 'all' → None (без ограничения)."""
    seconds = PERIOD_SECONDS.get(period)
    return None if seconds is None else now - seconds


def local_day_start(now: int, tz_name: str) -> int:
    """Unix-время начала текущих календарных суток (00:00) в часовом поясе чата."""
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")
    local = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(tz)
    midnight = tz.localize(datetime(local.year, local.month, local.day))
    return int(midnight.timestamp())


def mmr_series(matches: list[dict], step: int) -> list[tuple[int, int]]:
    """Накопленное изменение MMR после каждой игры: [(время, ±MMR)] (победа +step, поражение −step)."""
    total = 0
    points = []
    for match in sorted(matches, key=lambda m: m["start_time"]):
        total += step if is_win(match["player_slot"], match["radiant_win"]) else -step
        points.append((match["start_time"], total))
    return points


def _mean(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _group_stats(group: list[dict]) -> dict:
    games = len(group)
    wins = sum(1 for m in group if is_win(m["player_slot"], m["radiant_win"]))
    kills = sum(m.get("kills") or 0 for m in group)
    deaths = sum(m.get("deaths") or 0 for m in group)
    assists = sum(m.get("assists") or 0 for m in group)
    return {
        "games": games,
        "wins": wins,
        "losses": games - wins,
        "winrate": wins / games,
        "kda": (kills + assists) / max(deaths, 1),
        "avg_imp": _mean([m.get("imp") for m in group]),
        "avg_gpm": _mean([m.get("gpm") for m in group]),
    }


def hero_stats(
    matches: list[dict],
    since_ts: Optional[int] = None,
    limit: Optional[int] = None,
    hero_id: Optional[int] = None,
) -> list[dict]:
    """Статистика по героям (игры, винрейт, KDA, средний IMP/GPM), по убыванию числа игр."""
    by_hero: dict[int, list[dict]] = {}
    for m in matches:
        hid = m.get("hero_id")
        if not hid or (hero_id is not None and hid != hero_id):
            continue
        if since_ts is not None and m["start_time"] < since_ts:
            continue
        by_hero.setdefault(hid, []).append(m)
    result = [{"hero_id": hid, **_group_stats(group)} for hid, group in by_hero.items()]
    result.sort(key=lambda h: (h["games"], h["winrate"], -h["hero_id"]), reverse=True)
    return result[:limit] if limit else result


def role_stats(matches: list[dict], since_ts: Optional[int] = None) -> list[dict]:
    """Статистика по позициям 1–5 (только матчи с данными Stratz), по возрастанию позиции."""
    by_pos: dict[int, list[dict]] = {}
    for m in matches:
        pos = m.get("position")
        if not pos or (since_ts is not None and m["start_time"] < since_ts):
            continue
        by_pos.setdefault(pos, []).append(m)
    return [{"position": pos, **_group_stats(by_pos[pos])} for pos in sorted(by_pos)]
