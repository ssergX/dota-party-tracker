"""Рекорды пати за период: лучшая отдельная игра по каждому показателю (чистые функции).

compute_records([(имя, матчи)]) → по каждому показателю рекордсмен с героем, значением и id матча.
Период выбирает вызывающий код (матчи уже отфильтрованы по времени).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mmrbot.stats import is_win


@dataclass(frozen=True)
class RecordDef:
    key: str
    emoji: str
    title: str
    getter: Callable[[dict], Optional[float]]
    fmt: Callable[[float], str]
    anti: bool = False  # антирекорд (чем больше — тем «хуже»), но ищем максимум


def _kda(m: dict) -> Optional[float]:
    kills, deaths, assists = m.get("kills") or 0, m.get("deaths") or 0, m.get("assists") or 0
    if kills + assists < 10:  # без порога «0 смертей, 2 помощи» стало бы рекордом
        return None
    return (kills + assists) / max(deaths, 1)


def _n(v: float, one: str, few: str, many: str) -> str:
    n = int(v)
    n10, n100 = n % 10, n % 100
    word = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {word}"


def _k(v: float) -> str:
    return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"


RECORDS = [
    RecordDef("gpm", "💰", "Макс. GPM", lambda m: m.get("gpm"), lambda v: f"{v:.0f} GPM"),
    RecordDef("kills", "⚔️", "Больше всего убийств", lambda m: m.get("kills"), lambda v: _n(v, "убийство", "убийства", "убийств")),
    RecordDef("assists", "🤝", "Больше всего ассистов", lambda m: m.get("assists"), lambda v: _n(v, "ассист", "ассиста", "ассистов")),
    RecordDef("kda", "🎯", "Лучший KDA", _kda, lambda v: f"KDA {v:.1f}"),
    RecordDef("hero_damage", "💥", "Макс. урон по героям", lambda m: m.get("hero_damage"), lambda v: _k(v) + " урона"),
    RecordDef("tower_damage", "🏰", "Макс. урон по строениям", lambda m: m.get("tower_damage"), lambda v: _k(v) + " урона"),
    RecordDef("hero_healing", "💚", "Макс. лечение", lambda m: m.get("hero_healing") or None, lambda v: _k(v) + " лечения"),
    RecordDef("last_hits", "🌾", "Больше всего добиваний", lambda m: m.get("last_hits"), lambda v: f"{v:.0f} LH"),
    RecordDef("net_worth", "💎", "Макс. нетворт", lambda m: m.get("net_worth"), lambda v: _k(v) + " золота"),
    RecordDef("imp", "📊", "Лучший IMP", lambda m: m.get("imp"), lambda v: f"IMP {v:+.0f}"),
    RecordDef("duration", "⏳", "Самая долгая игра", lambda m: m.get("duration"), lambda v: f"{v / 60:.0f} мин"),
    RecordDef("deaths", "💀", "Больше всего смертей", lambda m: m.get("deaths"), lambda v: _n(v, "смерть", "смерти", "смертей"), anti=True),
]


def compute_records(named_matches: list[tuple[str, list[dict]]]) -> dict:
    """{'records': [{key,emoji,title,anti,player,value,text,match}], 'streak': (имя, длина)|None}.

    По каждому показателю — максимум среди всех игр пати за период (первый по времени при равенстве).
    """
    records = []
    for rec in RECORDS:
        best: Optional[tuple[float, str, dict]] = None
        for name, matches in named_matches:
            for match in matches:
                value = rec.getter(match)
                if value is None:
                    continue
                if best is None or value > best[0] or (value == best[0] and match["start_time"] < best[2]["start_time"]):
                    best = (value, name, match)
        if best is not None and (best[0] > 0 or rec.key == "imp"):
            value, name, match = best
            records.append({
                "key": rec.key, "emoji": rec.emoji, "title": rec.title, "anti": rec.anti,
                "player": name, "value": value, "text": rec.fmt(value), "match": match,
            })

    streak = None
    for name, matches in named_matches:
        longest = current = 0
        for match in sorted(matches, key=lambda m: m["start_time"]):
            if is_win(match["player_slot"], match["radiant_win"]):
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        if longest >= 2 and (streak is None or longest > streak[1]):
            streak = (name, longest)
    return {"records": records, "streak": streak}
