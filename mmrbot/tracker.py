"""Оркестровка: обновление игрока из OpenDota и сборка сводок/лидерборда.

Зависит от storage (данные), opendota-клиента (сеть) и stats (чистые расчёты).
Клиент передаётся аргументом — в тестах подставляется фейковый (без сети).
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from typing import Optional, Protocol

from mmrbot import achievements, party, records, stats
from mmrbot.ranks import rank_emoji, rank_label
from mmrbot.storage import Chat, Player, Storage

log = logging.getLogger(__name__)


ENRICH_CAP = 3  # матчей на refresh в запросе пользователя (~1с на матч); остальное — фоном, backfill_opendota
RECENT_GAME_SEC = 3 * 3600  # о матче старше этого окна не оповещаем (история при /add, простой бота)
GAME_REFRESH_COOLDOWN = 120  # сек: фоновая проверка новых игр не дёргает OpenDota чаще
REFRESH_COOLDOWN = 180  # сек: не ходить в OpenDota, если игрок обновлён недавно (скорость /stats)


class OpenDotaClient(Protocol):
    def refresh(self, account_id: int) -> bool: ...
    def get_profile(self, account_id: int) -> dict: ...
    def get_matches(self, account_id: int, limit: Optional[int] = 200) -> list[dict]: ...
    def get_totals(self, account_id: int) -> dict: ...
    def get_lanes(self, account_id: int) -> dict: ...
    def get_gpm_distribution(self, account_id: int) -> dict: ...
    def get_match_player_stats(self, match_id: int, account_id: int) -> Optional[dict]: ...


@dataclass
class PlayerSummary:
    display_name: str
    account_id: int
    rank: str
    anchor_mmr: Optional[int]
    current_mmr: Optional[int]
    mmr_delta: int
    games_total: int
    wins_total: int
    losses_total: int
    winrate: float
    kda_ratio: float
    avg_kills: float
    avg_deaths: float
    avg_assists: float
    games_today: int
    wins_today: int
    losses_today: int
    delta_today: int
    # расширенная статистика (пакеты A/C/D)
    rank_emoji: str = ""
    sum_kills: int = 0
    sum_deaths: int = 0
    sum_assists: int = 0
    streak_type: str = ""
    streak_len: int = 0
    top_heroes: list = field(default_factory=list)
    gpm: Optional[float] = None
    xpm: Optional[float] = None
    last_hits: Optional[float] = None
    avg_duration_min: float = 0.0
    max_duration_min: float = 0.0
    solo: tuple = (0, 0)
    party: tuple = (0, 0)
    best_hour: Optional[tuple] = None
    worst_hour: Optional[tuple] = None
    lanes: dict = field(default_factory=dict)
    recent_form: list = field(default_factory=list)
    best_game: Optional[dict] = None
    longest_win_streak: int = 0
    gpm_median: Optional[float] = None
    gpm_best: Optional[float] = None
    avg_perf: Optional[float] = None
    enriched_games: int = 0
    avg_gpm_window: Optional[float] = None
    avg_hero_damage_window: Optional[float] = None
    avg_net_worth_window: Optional[float] = None
    avg_last_hits_window: Optional[float] = None
    avg_hero_healing_window: Optional[float] = None
    skill: dict = field(default_factory=dict)
    role_style: str = ""
    lobby_rank: Optional[int] = None
    hero_pool: int = 0
    wins_losses: dict = field(default_factory=dict)
    steam_name: Optional[str] = None


def _normalize(raw: dict) -> dict:
    return {
        "match_id": raw["match_id"],
        "start_time": raw["start_time"],
        "player_slot": raw["player_slot"],
        "radiant_win": raw["radiant_win"],
        "lobby_type": raw.get("lobby_type"),
        "kills": raw.get("kills", 0) or 0,
        "deaths": raw.get("deaths", 0) or 0,
        "assists": raw.get("assists", 0) or 0,
        "hero_id": raw.get("hero_id"),
        "duration": raw.get("duration"),
        "party_size": raw.get("party_size"),
        "average_rank": raw.get("average_rank"),
    }


class StratzClient(Protocol):
    def get_matches(self, account_id: int, match_ids: list[int]) -> dict[int, dict]: ...


HISTORY_LIMIT_FIRST = None  # первая загрузка — вся ранкед-история игрока
HISTORY_LIMIT_REFRESH = 200  # дальше хватает свежих матчей
STRATZ_CAP = 50  # сколько матчей дозаполнять у Stratz за один refresh (пачкой, 1–2 запроса)


def _enrich_from_stratz(storage: Storage, stratz: StratzClient, player: Player) -> None:
    """Дозаполнить позицию/роль/лейн/IMP. Нет недостающих матчей — нет и запроса."""
    pending = storage.get_match_ids_without_stratz(player.id, 0, STRATZ_CAP)
    if not pending:
        return
    try:
        data = stratz.get_matches(player.account_id, pending)
    except Exception:
        log.warning("Stratz недоступен для игрока %s, пропускаю", player.account_id, exc_info=True)
        return
    for match_id in pending:
        if match_id in data:
            storage.update_match_stratz(player.id, match_id, data[match_id])
    # Матч без ответа или без размера пати считаем промахом — чтобы очередь не крутилась вечно.
    storage.mark_stratz_miss(player.id, [m for m in pending if (data.get(m) or {}).get("party_size") is None])


def backfill_stratz(storage: Storage, stratz: StratzClient, rounds: int = 6) -> int:
    """Фоновое дозаполнение позиций/IMP/статы по всем игрокам (rounds пачек по STRATZ_CAP на игрока)."""
    done = 0
    seen: set[int] = set()
    for chat in storage.list_chats():
        for player in storage.list_players(chat.chat_id):
            if player.account_id in seen:
                continue
            seen.add(player.account_id)
            for _ in range(rounds):
                before = len(storage.get_match_ids_without_stratz(player.id, 0, STRATZ_CAP))
                if not before:
                    break
                _enrich_from_stratz(storage, stratz, player)
                done += before
    return done


def _enrich_from_opendota(storage: Storage, client: OpenDotaClient, player: Player, cap: int) -> int:
    """Пер-матч поля + role-normalized perf из benchmarks (по 1 GET на матч). Вернуть число запросов."""
    ids = storage.get_unenriched_match_ids(player.id, 0, cap)
    for match_id in ids:
        try:
            details = client.get_match_player_stats(match_id, player.account_id)
        except Exception:
            log.debug("Не удалось обогатить матч %s", match_id, exc_info=True)
            storage.mark_enrich_miss(player.id, match_id)
            continue
        if not details:
            storage.mark_enrich_miss(player.id, match_id)
            continue
        ps = stats.perf_score(details.get("benchmarks") or {})
        storage.update_match_details(player.id, match_id, details, ps)
    return len(ids)


def backfill_opendota(storage: Storage, client: OpenDotaClient, per_player: int = 30) -> int:
    """Фоновое обогащение бэклога матчей (perf/benchmarks) — не в пути пользовательской команды."""
    done = 0
    seen: set[int] = set()
    for chat in storage.list_chats():
        for player in storage.list_players(chat.chat_id):
            if player.account_id in seen:
                continue
            seen.add(player.account_id)
            done += _enrich_from_opendota(storage, client, player, per_player)
    return done


def detect_steam_changes(storage: Storage, client: OpenDotaClient) -> list[dict]:
    """Обойти всех игроков, обновить сохранённые ник/аватарку Steam и вернуть смены для оповещений.

    Профиль аккаунта запрашивается один раз, даже если он добавлен в несколько чатов. Базовые значения
    обновляются и в чатах с выключенными оповещениями — чтобы при включении не пришла «пачка» старых смен.
    """
    events: list[dict] = []
    profiles: dict[int, Optional[dict]] = {}
    for chat in storage.list_chats():
        for player in storage.list_players(chat.chat_id):
            if player.account_id not in profiles:
                try:
                    profiles[player.account_id] = client.get_profile(player.account_id)
                except Exception:
                    log.warning("Не удалось получить Steam-профиль %s", player.account_id, exc_info=True)
                    profiles[player.account_id] = None
            profile = profiles[player.account_id]
            if not profile:
                continue
            changes = storage.update_player_steam(player.id, profile.get("personaname"), profile.get("avatarfull"))
            if changes and chat.notify_steam:
                events.append({"chat_id": chat.chat_id, "player": player, "changes": changes, "profile": profile})
    return events


def check_achievements(storage: Storage, player: Player, now: int) -> list[tuple[str, Optional[str]]]:
    """Пересчитать достижения игрока; вернуть новые [(code, деталь)].

    Первая проверка игрока только запоминает уже заработанное (без оповещений, чтобы не «завалить» чат).
    """
    earned = achievements.evaluate(storage.get_matches(player.id))
    known = storage.get_achievements(player.id)
    if "_seeded" not in known:
        storage.add_achievements(player.id, dict(earned, _seeded=None), now)
        return []
    new = {code: detail for code, detail in earned.items() if code not in known}
    if new:
        storage.add_achievements(player.id, new, now)
    return list(new.items())


def list_achievements(storage: Storage, chat_id: int, now: int, name: Optional[str] = None) -> list[tuple]:
    """[(имя, {code: (ts, деталь)})] по игрокам чата (или одному): заработанное сейчас, с датой из БД."""
    players = storage.list_players(chat_id)
    if name is not None:
        player = storage.get_player(chat_id, name)
        players = [player] if player else []
    result = []
    for player in players:
        earned = achievements.evaluate(storage.get_matches(player.id))
        stored = storage.get_achievements(player.id)
        result.append((player.display_name, {
            code: (stored[code][0] if code in stored else now, detail) for code, detail in earned.items()
        }))
    return result


def shared_games_since(storage: Storage, chat_id: int, since_ts: int) -> dict:
    """Совместные игры пати (≥2 игрока на одной стороне) начиная с since_ts: games/wins/losses."""
    named = [(p.display_name, storage.get_matches(p.id, since_ts=since_ts)) for p in storage.list_players(chat_id)]
    return party.together_summary(named)


def detect_new_games(
    storage: Storage, client: OpenDotaClient, chat: Chat, now: int, stratz: Optional[StratzClient] = None,
) -> list[dict]:
    """Фоновая проверка чата: события «новая игра» (по матчам, вместе игравшие — одним) и «достижение».

    Оповещаем только о матчах не старше RECENT_GAME_SEC; всё остальное молча помечается обработанным.
    """
    if not chat.notify_games:
        return []
    by_match: dict[int, dict] = {}
    events: list[dict] = []
    for player in storage.list_players(chat.chat_id):
        stale = player.updated_ts is None or (now - player.updated_ts) >= GAME_REFRESH_COOLDOWN
        if stale:
            try:
                refresh_player(storage, client, player, now, stratz=stratz)
            except Exception:
                log.warning("Фоновое обновление игрока %s не удалось", player.display_name, exc_info=True)
            player = storage.get_player_by_account_id(chat.chat_id, player.account_id) or player
        fresh = storage.get_unnotified_matches(player.id, now - RECENT_GAME_SEC)
        storage.mark_notified(player.id)
        if not fresh:
            continue
        summary = build_player_summary(storage, chat, player, now)
        for match in fresh:
            entry = by_match.setdefault(match["match_id"], {
                "kind": "match", "chat_id": chat.chat_id, "match_id": match["match_id"],
                "start_time": match["start_time"], "duration": match.get("duration"), "rows": [], "shared": None,
            })
            entry["rows"].append({
                "name": player.display_name, "hero_id": match.get("hero_id"),
                "kills": match.get("kills") or 0, "deaths": match.get("deaths") or 0,
                "assists": match.get("assists") or 0,
                "won": stats.is_win(match["player_slot"], match["radiant_win"]),
                "step": chat.mmr_step, "current_mmr": summary.current_mmr,
                "streak_type": summary.streak_type, "streak_len": summary.streak_len,
            })
        new_ach = check_achievements(storage, player, now)
        if new_ach:
            events.append({"kind": "achievement", "chat_id": chat.chat_id, "name": player.display_name, "items": new_ach})
    matches_events = sorted(by_match.values(), key=lambda e: e["start_time"])
    for entry in matches_events:
        if len(entry["rows"]) >= 2:
            day_start = stats.local_day_start(now, chat.tz)
            entry["shared"] = shared_games_since(storage, chat.chat_id, day_start)
    return matches_events + events


def build_records(storage: Storage, chat_id: int, since_ts: Optional[int]) -> dict:
    """Рекорды пати за период (из кэша БД, вся сохранённая история); since_ts=None — за всё время."""
    named = [(p.display_name, storage.get_matches(p.id, since_ts=since_ts)) for p in storage.list_players(chat_id)]
    return records.compute_records(named)


def build_weekly_report(storage: Storage, chat_id: int, now: int) -> dict:
    """Итоги недели чата из кэша БД: таблица периода, герой недели, лучшая серия, совместные игры."""
    chat = storage.get_or_create_chat(chat_id)
    since = now - 7 * 86_400
    rows = build_period_leaderboard(storage, chat_id, since)
    players = storage.list_players(chat_id)
    named = [(p.display_name, storage.get_matches(p.id, since_ts=since)) for p in players]
    by_hero: dict[int, list[int]] = {}
    best_streak = ("", 0)
    for name, matches in named:
        for m in matches:
            if m.get("hero_id") is not None:
                stat = by_hero.setdefault(m["hero_id"], [0, 0])
                stat[0] += 1
                stat[1] += 1 if stats.is_win(m["player_slot"], m["radiant_win"]) else 0
        streak = stats.longest_win_streak(matches)
        if streak > best_streak[1]:
            best_streak = (name, streak)
    hero = None
    if by_hero:
        hero_id, (games, wins) = max(by_hero.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        hero = {"hero_id": hero_id, "games": games, "wins": wins}
    return {
        "rows": rows,
        "hero": hero,
        "streak": best_streak if best_streak[1] >= 2 else None,
        "shared": party.together_summary(named),
        "step": chat.mmr_step,
    }


def build_mmr_series(storage: Storage, chat_id: int, since_ts: Optional[int]) -> dict[str, list[tuple[int, int]]]:
    """Динамика оценки MMR (накопленное ±) по игрокам чата: {имя: [(время, Δ)]}; без игр — игрок пропущен.

    since_ts — граница периода (игры берутся из всей сохранённой истории); None («всё») — вся история.
    """
    chat = storage.get_or_create_chat(chat_id)
    result: dict[str, list[tuple[int, int]]] = {}
    for player in storage.list_players(chat_id):
        start = since_ts if since_ts is not None else 0
        points = stats.mmr_series(storage.get_matches(player.id, since_ts=start), chat.mmr_step)
        if points:
            result[player.display_name] = points
    return result


def refresh_player(
    storage: Storage, client: OpenDotaClient, player: Player, now: int,
    stratz: Optional[StratzClient] = None,
) -> int:
    """Подтянуть ранкед-историю и текущий ранг. Вернуть число новых матчей."""
    # Пнуть OpenDota перечитать историю — свежие игры доедут быстрее (best-effort).
    try:
        client.refresh(player.account_id)
    except Exception:
        pass

    profile = client.get_profile(player.account_id)
    if profile is not None:
        storage.update_player_rank(
            player.id,
            rank_tier=profile.get("rank_tier"),
            leaderboard_rank=profile.get("leaderboard_rank"),
            updated_ts=now,
        )

    # Храним ВСЮ ранкед-историю (для героев/позиций/матчей за любой период). Окно «с момента
    # добавления» применяется только в лидерборде (build_player_summary). Первая загрузка —
    # глубже, дальше хватает свежих матчей.
    has_history = bool(storage.get_matches(player.id))
    raw_matches = client.get_matches(
        player.account_id, limit=HISTORY_LIMIT_REFRESH if has_history else HISTORY_LIMIT_FIRST
    )
    fresh = [
        _normalize(m)
        for m in raw_matches
        if stats.is_ranked_lobby(m.get("lobby_type"))
        and m.get("radiant_win") is not None  # исход неизвестен → не считаем матч
    ]
    inserted = storage.add_matches(player.id, fresh)

    # Агрегаты (totals/линии/GPM) меняются только с новыми матчами. Если их нет, а инсайты уже
    # сохранены — пропускаем 3 запроса (каждый ≈1.1 с троттлинга OpenDota).
    insights_fresh = inserted == 0 and player.last_lanes is not None
    if not insights_fresh:
        # Средние GPM/XPM/last hits (пакет D) — необязательный доп. запрос.
        try:
            totals = client.get_totals(player.account_id)
            storage.update_player_totals(
                player.id, totals.get("gpm"), totals.get("xpm"), totals.get("last_hits")
            )
        except Exception:
            log.debug("Не удалось получить totals игрока %s", player.account_id, exc_info=True)

        # Линии + распределение GPM — тоже необязательные доп. запросы.
        try:
            lanes = client.get_lanes(player.account_id)
            lanes_json = json.dumps({str(lane): list(gw) for lane, gw in lanes.items()})
            dist = client.get_gpm_distribution(player.account_id)
            storage.update_player_insights(player.id, lanes_json, dist.get("median"), dist.get("best"))
        except Exception:
            log.debug("Не удалось получить lanes/gpm игрока %s", player.account_id, exc_info=True)

    _enrich_from_opendota(storage, client, player, ENRICH_CAP)

    if stratz is not None:
        _enrich_from_stratz(storage, stratz, player)

    return inserted


def build_player_summary(storage: Storage, chat: Chat, player: Player, now: int) -> PlayerSummary:
    step = chat.mmr_step

    all_matches = storage.get_matches(player.id)  # вся ранкед-история; MMR-оценка — от якоря ниже
    anchor_matches = storage.get_matches(player.id, since_ts=player.anchor_ts)
    today_matches = storage.get_matches(player.id, since_ts=stats.local_day_start(now, chat.tz))

    agg_all = stats.aggregate(all_matches)
    agg_anchor = stats.aggregate(anchor_matches)
    agg_today = stats.aggregate(today_matches)

    mmr_delta = stats.estimate_mmr_delta(agg_anchor.wins, agg_anchor.losses, step)
    current_mmr = player.anchor_mmr + mmr_delta if player.anchor_mmr is not None else None
    delta_today = stats.estimate_mmr_delta(agg_today.wins, agg_today.losses, step)

    streak_type, streak_len = stats.current_streak(all_matches)
    top = stats.top_heroes(all_matches, k=3)
    duration = stats.duration_stats(all_matches)
    split = stats.solo_party_split(all_matches)
    best_hour, worst_hour = _best_worst_hour(stats.winrate_by_hour(all_matches, chat.tz))
    lanes = _parse_lanes(player.last_lanes)

    enriched = [m for m in all_matches if m.get("perf_score") is not None]
    avg_perf = sum(m["perf_score"] for m in enriched) / len(enriched) if enriched else None

    def _mean_field(field: str) -> Optional[float]:
        values = [m[field] for m in all_matches if m.get(field) is not None]
        return sum(values) / len(values) if values else None

    bench_list = []
    for match in all_matches:
        raw = match.get("bench_json")
        if raw:
            try:
                bench_list.append(json.loads(raw))
            except (ValueError, TypeError):
                pass
    skill = stats.aggregate_skill(bench_list)
    avg_last_hits_window = _mean_field("last_hits")
    avg_hero_healing_window = _mean_field("hero_healing")
    role_style = stats.infer_role_style(avg_last_hits_window, avg_hero_healing_window)

    lobby_ranks = [m["average_rank"] for m in all_matches if m.get("average_rank")]
    lobby_rank = round(statistics.median(lobby_ranks)) if lobby_ranks else None
    wins_losses = stats.wins_losses_split(all_matches)
    pool = stats.hero_pool(all_matches)

    return PlayerSummary(
        display_name=player.display_name,
        steam_name=player.steam_name,
        account_id=player.account_id,
        rank=rank_label(player.last_rank_tier, player.last_leaderboard_rank),
        rank_emoji=rank_emoji(player.last_rank_tier),
        anchor_mmr=player.anchor_mmr,
        current_mmr=current_mmr,
        mmr_delta=mmr_delta,
        games_total=agg_all.games,
        wins_total=agg_all.wins,
        losses_total=agg_all.losses,
        winrate=agg_all.winrate,
        kda_ratio=agg_all.kda_ratio,
        avg_kills=agg_all.avg_kills,
        avg_deaths=agg_all.avg_deaths,
        avg_assists=agg_all.avg_assists,
        games_today=agg_today.games,
        wins_today=agg_today.wins,
        losses_today=agg_today.losses,
        delta_today=delta_today,
        sum_kills=agg_all.sum_kills,
        sum_deaths=agg_all.sum_deaths,
        sum_assists=agg_all.sum_assists,
        streak_type=streak_type,
        streak_len=streak_len,
        top_heroes=top,
        gpm=player.last_gpm,
        xpm=player.last_xpm,
        last_hits=player.last_last_hits,
        avg_duration_min=duration["avg_minutes"],
        max_duration_min=duration["max_minutes"],
        solo=split["solo"],
        party=split["party"],
        best_hour=best_hour,
        worst_hour=worst_hour,
        lanes=lanes,
        recent_form=stats.recent_form(all_matches, 5),
        best_game=stats.best_game(all_matches),
        longest_win_streak=stats.longest_win_streak(all_matches),
        gpm_median=player.last_gpm_median,
        gpm_best=player.last_gpm_best,
        avg_perf=avg_perf,
        enriched_games=len(enriched),
        avg_gpm_window=_mean_field("gpm"),
        avg_hero_damage_window=_mean_field("hero_damage"),
        avg_net_worth_window=_mean_field("net_worth"),
        avg_last_hits_window=avg_last_hits_window,
        avg_hero_healing_window=avg_hero_healing_window,
        skill=skill,
        role_style=role_style,
        lobby_rank=lobby_rank,
        hero_pool=pool,
        wins_losses=wins_losses,
    )


def build_leaderboard(
    storage: Storage,
    client: OpenDotaClient,
    chat_id: int,
    now: int,
    refresh: bool = True,
    stratz: Optional[StratzClient] = None,
) -> list[PlayerSummary]:
    chat = storage.get_or_create_chat(chat_id)
    players = storage.list_players(chat_id)

    summaries: list[PlayerSummary] = []
    for player in players:
        # Кулдаун: если обновляли недавно — берём кэш из БД, не дёргаем OpenDota (скорость).
        stale = player.updated_ts is None or (now - player.updated_ts) >= REFRESH_COOLDOWN
        if refresh and stale:
            try:
                refresh_player(storage, client, player, now, stratz=stratz)
            except Exception:  # ошибка по одному игроку не должна рушить весь лидерборд
                log.warning("Не удалось обновить игрока %s (id %s), беру кэш",
                            player.display_name, player.account_id, exc_info=True)
            # точная перечитка по account_id (без неоднозначности имён)
            player = storage.get_player_by_account_id(chat_id, player.account_id) or player
        summaries.append(build_player_summary(storage, chat, player, now))

    # По убыванию текущего MMR; игроки без оценки MMR — в конце.
    summaries.sort(key=lambda s: (s.current_mmr is not None, s.current_mmr or 0), reverse=True)
    return summaries


def build_period_leaderboard(storage: Storage, chat_id: int, since_ts: Optional[int]) -> list[dict]:
    """Итоги игроков чата за период (из кэша БД): игры, победы, оценка MMR-дельты, KDA.

    Сначала те, кто играл (по дельте, затем по числу игр); без игр — в конце.
    """
    chat = storage.get_or_create_chat(chat_id)
    rows: list[dict] = []
    for player in storage.list_players(chat_id):
        agg = stats.aggregate(storage.get_matches(player.id, since_ts=since_ts))
        rows.append({
            "name": player.display_name,
            "games": agg.games,
            "wins": agg.wins,
            "losses": agg.losses,
            "delta": stats.estimate_mmr_delta(agg.wins, agg.losses, chat.mmr_step),
            "winrate": agg.winrate,
            "kda": agg.kda_ratio,
        })
    rows.sort(key=lambda r: (r["games"] > 0, r["delta"], r["games"]), reverse=True)
    return rows


def build_match_view(
    storage: Storage, chat_id: int, name: Optional[str], match_id: Optional[int]
) -> Optional[dict]:
    """Данные для карточки матча из кэша БД.

    name/match_id опциональны: без имени берём игрока с самым свежим матчем (в чате),
    без match_id — его последний матч.
    """
    players = storage.list_players(chat_id)
    if name:
        target = storage.get_player(chat_id, name)
        if target is None:
            return None
        candidates = [target]
    else:
        candidates = players

    best = None  # (start_time, player, row)
    for player in candidates:
        for row in storage.get_matches(player.id):
            if match_id is not None and row["match_id"] != match_id:
                continue
            if best is None or row["start_time"] > best[0]:
                best = (row["start_time"], player, row)
    if best is None:
        return None
    _, player, row = best
    return {"player": player, "match": row}


def build_player_heroes(
    storage: Storage, chat_id: int, name: str, since_ts: Optional[int]
) -> Optional[tuple[Player, list[dict]]]:
    player = storage.get_player(chat_id, name)
    if player is None:
        return None
    return player, stats.hero_stats(storage.get_matches(player.id), since_ts=since_ts)


def build_player_roles(
    storage: Storage, chat_id: int, name: str, since_ts: Optional[int]
) -> Optional[tuple[Player, list[dict]]]:
    player = storage.get_player(chat_id, name)
    if player is None:
        return None
    return player, stats.role_stats(storage.get_matches(player.id), since_ts=since_ts)


def build_hero_view(
    storage: Storage, chat_id: int, hero_id: int, since_ts: Optional[int]
) -> list[tuple[Player, dict]]:
    """Кто из пати играл на герое: [(игрок, статистика)] по убыванию числа игр."""
    entries = []
    for player in storage.list_players(chat_id):
        rows = stats.hero_stats(storage.get_matches(player.id), since_ts=since_ts, hero_id=hero_id)
        if rows:
            entries.append((player, rows[0]))
    entries.sort(key=lambda e: (e[1]["games"], e[1]["winrate"]), reverse=True)
    return entries


def _parse_lanes(lanes_json: Optional[str]) -> dict:
    """JSON {'2':[games,wins]} → {2: (games, wins)}."""
    if not lanes_json:
        return {}
    try:
        raw = json.loads(lanes_json)
    except (ValueError, TypeError):
        return {}
    result = {}
    for key, value in raw.items():
        try:
            result[int(key)] = (value[0], value[1])
        except (ValueError, TypeError, IndexError):
            continue
    return result


def _best_worst_hour(by_hour: dict, min_games: int = 3):
    """Из {час: (игр, побед)} выбрать лучший/худший час (по винрейту, порог по играм)."""
    qualified = [(hour, wins / games, games) for hour, (games, wins) in by_hour.items() if games >= min_games]
    if not qualified:
        return (None, None)
    best = max(qualified, key=lambda x: x[1])
    worst = min(qualified, key=lambda x: x[1])
    return ((best[0], best[1]), (worst[0], worst[1]))


def compute_awards(summaries: list[PlayerSummary], min_games: int = 3) -> list[dict]:
    """Награды, рассказывающие историю пати (не липнут к одному игроку). {title, player, detail}.

    Позитивные — лучшему; «главный тилт» — тому, кто РЕАЛЬНО в просадке (серия поражений),
    а не самому активному. Метрики по ставкам/сериям, не по абсолютным суммам.
    """
    eligible = [s for s in summaries if s.games_total >= min_games]
    awards: list[dict] = []
    if not eligible:
        return awards

    # MVP по role-normalized перформансу (честнее KDA) — самый престижный.
    perf_eligible = [s for s in eligible if s.avg_perf is not None]
    if perf_eligible:
        mvp = max(perf_eligible, key=lambda s: s.avg_perf)
        awards.append({"title": "Наивысший перформанс", "player": mvp.display_name,
                       "detail": f"{mvp.avg_perf * 100:.0f}/100"})

    king = max(eligible, key=lambda s: s.winrate)
    awards.append({"title": "Наивысший винрейт", "player": king.display_name,
                   "detail": f"{king.winrate * 100:.0f}% ({king.wins_total}–{king.losses_total})"})

    # Стилевые награды (по ставкам/за игру) — разводят кор/саппорт/дамагера.
    def _best(getter, title, detail):
        pool = [s for s in eligible if getter(s) is not None]
        if pool:
            top = max(pool, key=getter)
            awards.append({"title": title, "player": top.display_name, "detail": detail(top)})

    _best(lambda s: s.avg_gpm_window, "Наибольший GPM", lambda s: f"{s.avg_gpm_window:.0f} GPM в среднем за игру")
    _best(lambda s: s.avg_hero_damage_window, "Наибольший урон по героям",
          lambda s: f"{s.avg_hero_damage_window / 1000:.1f}k урона/игра")
    _best(lambda s: s.avg_assists or None, "Наибольшее число ассистов", lambda s: f"{s.avg_assists:.0f} ассистов/игра")
    _best(lambda s: s.best_game["kda"] if s.best_game else None, "Лучшая отдельная игра",
          lambda s: f"{s.best_game['kills']}/{s.best_game['deaths']}/{s.best_game['assists']}")
    _best(lambda s: s.hero_pool or None, "Самый широкий пул героев", lambda s: f"{s.hero_pool} героев")

    # На кураже — самая длинная текущая серия ПОБЕД.
    hot = [s for s in eligible if s.streak_type == "W" and s.streak_len >= 2]
    if hot:
        top = max(hot, key=lambda s: s.streak_len)
        awards.append({"title": "Текущая серия побед", "player": top.display_name,
                       "detail": f"{top.streak_len} побед подряд"})

    # Камикадзе — больше всего смертей ЗА ИГРУ (не сумма! честно к активности).
    _best(lambda s: s.avg_deaths or None, "Наибольшее число смертей", lambda s: f"{s.avg_deaths:.0f} смертей/игра")

    # Главный тилт — самая длинная серия ПОРАЖЕНИЙ (реальная просадка).
    cold = [s for s in eligible if s.streak_type == "L" and s.streak_len >= 2]
    if cold:
        bottom = max(cold, key=lambda s: (s.streak_len, -s.winrate))
        awards.append({"title": "Текущая серия поражений", "player": bottom.display_name,
                       "detail": f"{bottom.streak_len} поражений подряд, {bottom.winrate * 100:.0f}%"})

    return awards


# Метрики для сравнения игроков внутри чата (все — «выше = лучше»).
_COMPARE_METRICS = {
    "perf": lambda s: s.avg_perf,
    "winrate": lambda s: s.winrate if s.games_total else None,
    "kda": lambda s: s.kda_ratio if s.games_total else None,
    "gpm": lambda s: s.avg_gpm_window,
}


def build_chat_comparison(summaries: list[PlayerSummary]) -> dict:
    """Ранги игроков внутри чата по метрикам + композитная «сила в чате».

    Для каждой метрики ранжируем (1 = лучший). power = среднее нормированной позиции
    (1=лучший…0=худший) по метрикам, где значение есть. power_rank — итоговое место.
    """
    players: dict = {s.display_name: {"ranks": {}, "leads": [], "_pos": []} for s in summaries}
    averages: dict = {}

    for key, getter in _COMPARE_METRICS.items():
        valued = [(s.display_name, getter(s)) for s in summaries if getter(s) is not None]
        present = [value for _, value in valued]
        averages[key] = sum(present) / len(present) if present else None
        ordered = sorted(valued, key=lambda kv: kv[1], reverse=True)
        n = len(ordered)
        for rank, (name, _value) in enumerate(ordered, start=1):
            players[name]["ranks"][key] = rank
            if rank == 1 and n > 1:
                players[name]["leads"].append(key)
            players[name]["_pos"].append((n - rank) / (n - 1) if n > 1 else 1.0)

    ranking = []
    for name, data in players.items():
        positions = data.pop("_pos")
        data["power"] = sum(positions) / len(positions) if positions else None
        ranking.append((name, data["power"]))
    ranking.sort(key=lambda kv: (kv[1] is not None, kv[1] or 0), reverse=True)
    for rank, (name, _power) in enumerate(ranking, start=1):
        players[name]["power_rank"] = rank

    return {"size": len(summaries), "players": players, "averages": averages}


def build_together(storage: Storage, chat_id: int) -> dict:
    """Статистика совместной игры пати (пакет B)."""
    players = storage.list_players(chat_id)
    named_matches = [
        (p.display_name, storage.get_matches(p.id)) for p in players
    ]
    return {
        "player_count": len(players),
        "summary": party.together_summary(named_matches),
        "duo": party.best_duo(named_matches),
    }
