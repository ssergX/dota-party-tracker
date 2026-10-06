"""Оркестровка: обновление игрока из OpenDota и сборка сводок/лидерборда.

Зависит от storage (данные), opendota-клиента (сеть) и stats (чистые расчёты).
Клиент передаётся аргументом — в тестах подставляется фейковый (без сети).
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Protocol

from mmrbot import achievements, party, records, stats
from mmrbot.presence import advance
from mmrbot.awards import compute_period_awards
from mmrbot.ranks import mmr_rank_mismatch, rank_emoji, rank_label
from mmrbot.storage import Chat, Player, Storage

log = logging.getLogger(__name__)


ENRICH_CAP = 3  # матчей на refresh в запросе пользователя (~1с на матч); остальное — фоном, backfill_opendota
RECENT_GAME_SEC = 3 * 3600  # о матче старше этого окна не оповещаем (история при /add, простой бота)
GAME_REFRESH_COOLDOWN = 120  # сек: фоновая проверка новых игр не дёргает OpenDota чаще
GAME_IDLE_COOLDOWN = 600  # сек: то же, когда пати давно не играла и ключа OpenDota нет (бережём лимит запросов)
ACTIVE_WINDOW = 3 * 3600  # сек после последней игры, пока пати считается «в сессии» (опрос частый)
PROFILE_TTL = 6 * 3600  # сек: ранг без новых игр перечитываем не чаще
DEEP_SYNC_SEC = 6 * 3600  # сек: раз в столько сверяем историю глубоко (200 матчей), между — лёгкий список
REFRESH_WORKERS = 4  # игроков обновляем параллельно (частоту запросов держит троттлинг клиента)
REFRESH_COOLDOWN = 180  # сек: не ходить в OpenDota, если игрок обновлён недавно (скорость /stats)


class OpenDotaClient(Protocol):
    def refresh(self, account_id: int) -> bool: ...
    def get_profile(self, account_id: int) -> dict: ...
    def get_matches(self, account_id: int, limit: Optional[int] = 200) -> list[dict]: ...
    # get_recent_matches(account_id) -> list[dict] — необязателен: есть у настоящего клиента, лёгкая сверка
    def get_match_player_stats(
        self, match_id: int, account_id: int, player_slot: Optional[int] = None
    ) -> Optional[dict]: ...


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
    party_unknown: tuple = (0, 0)  # игры, где размер пати неизвестен
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
    detail_games: int = 0  # игр, по которым известны GPM/нетворт/урон (средние считаются только по ним)
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
    anchor_games: int = 0  # игр, по которым считается ±MMR (с момента задания MMR)
    mmr_drift: bool = False  # оценка MMR разошлась с медалью — стоит обновить /setmmr
    history_closed: bool = False  # история матчей закрыта у OpenDota — цифры могут быть неполными


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
        # Есть только в /recentMatches: статистика матча без отдельного запроса на матч.
        "gpm": raw.get("gold_per_min"),
        "xpm": raw.get("xp_per_min"),
        "last_hits": raw.get("last_hits"),
        "hero_damage": raw.get("hero_damage"),
        "tower_damage": raw.get("tower_damage"),
        "hero_healing": raw.get("hero_healing"),
        "leaver_status": raw.get("leaver_status"),
    }


class StratzClient(Protocol):
    def get_matches(
        self, account_id: int, match_ids: list[int], hints: Optional[dict] = None
    ) -> dict[int, dict]: ...


HISTORY_LIMIT_FIRST = None  # первая загрузка — вся ранкед-история игрока
HISTORY_LIMIT_REFRESH = 200  # дальше хватает свежих матчей
STRATZ_CAP = 50  # сколько матчей дозаполнять у Stratz за один refresh (пачкой, 1–2 запроса)


def _enrich_from_stratz(
    storage: Storage, stratz: StratzClient, player: Player, now: Optional[int] = None
) -> None:
    """Дозаполнить позицию/роль/лейн/IMP. Нет недостающих матчей — нет и запроса.

    Промах (Stratz ещё не разобрал матч) откладывает повтор с растущей паузой, а не сжигает попытки подряд.
    """
    now = int(time.time()) if now is None else now
    pending = storage.get_match_ids_without_stratz(player.id, 0, STRATZ_CAP, now=now)
    if not pending:
        return
    try:
        data = stratz.get_matches(player.account_id, pending, hints=storage.get_match_hints(player.id, pending))
    except Exception:
        log.warning("Stratz недоступен для игрока %s, пропускаю", player.account_id, exc_info=True)
        return
    for match_id in pending:
        if match_id in data:
            storage.update_match_stratz(player.id, match_id, data[match_id])
    # Матч без ответа или без размера пати считаем промахом — чтобы очередь не крутилась вечно.
    storage.mark_stratz_miss(player.id, [m for m in pending if (data.get(m) or {}).get("party_size") is None], now)


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
                before = len(storage.get_match_ids_without_stratz(player.id, 0, STRATZ_CAP, now=int(time.time())))
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
            details = client.get_match_player_stats(
                match_id, player.account_id, storage.get_match_slot(player.id, match_id)
            )
        except Exception:  # сбой/лимит — не «нет данных»: попытку не тратим и остальные матчи не дёргаем
            log.debug("OpenDota не ответил по матчу %s, остановка прогона", match_id, exc_info=True)
            break
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


def _store_profile(storage: Storage, player: Player, profile: dict, now: int) -> None:
    """Сохранить ранг из профиля. Пустой профиль (сбой/скрытый аккаунт) известный ранг не затирает."""
    tier, board = profile.get("rank_tier"), profile.get("leaderboard_rank")
    if tier is None and not profile.get("personaname"):
        tier, board = player.last_rank_tier, player.last_leaderboard_rank
    closed = profile.get("fh_unavailable")
    storage.set_player_rank(player.id, tier, board, now, None if closed is None else bool(closed))


def finish_refresh(storage: Storage, client: OpenDotaClient, chat_id: int, per_player: int = ENRICH_CAP) -> int:
    """Вторая, необязательная для ответа половина обновления чата — выполняется фоном после команды.

    Быстрое обновление (refresh_player(fast=True)) забирает матчи, ранг и позиции Stratz. Здесь
    догоняем медленное у OpenDota: пинок перечитать историю и perf/benchmarks свежих матчей.
    (Карьерные средние GPM/XPM, линии и медиану GPM больше не запрашиваем: бот их нигде не показывает.) Нечего догонять — запросов нет. Вернуть число запросов деталей матчей.
    """
    done = 0
    for player in storage.list_players(chat_id):
        try:
            client.refresh(player.account_id)
        except Exception:
            pass
        done += _enrich_from_opendota(storage, client, player, per_player)
    return done


def refresh_heroes(client: OpenDotaClient) -> int:
    """Подтянуть справочник героев с OpenDota (новый герой — не «hero 156»). Вернуть число новых героев."""
    from mmrbot.heroes import update_heroes
    return update_heroes(client.get_heroes())


def detect_steam_changes(storage: Storage, client: OpenDotaClient, now: Optional[int] = None) -> list[dict]:
    """Обойти всех игроков, обновить сохранённые ник/аватарку Steam и вернуть смены для оповещений.

    Профиль аккаунта запрашивается один раз, даже если он добавлен в несколько чатов. Базовые значения
    обновляются и в чатах с выключенными оповещениями — чтобы при включении не пришла «пачка» старых смен.
    Из того же ответа сохраняется ранг — отдельный запрос профиля при обновлении матчей не нужен.
    """
    now = int(time.time()) if now is None else now
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
            _store_profile(storage, player, profile, now)
            changes = storage.update_player_steam(player.id, profile.get("personaname"), profile.get("avatarfull"))
            if changes and chat.notify_steam:
                events.append({"chat_id": chat.chat_id, "player": player, "changes": changes, "profile": profile})
    return events


def check_achievements(storage: Storage, player: Player, now: int) -> list[tuple[str, Optional[str]]]:
    """Пересчитать достижения игрока; вернуть новые [(code, деталь)].

    Первая проверка игрока только запоминает уже заработанное (без оповещений, чтобы не «завалить» чат).
    """
    earned = achievements.evaluate_timed(storage.get_matches(player.id))
    known = storage.get_achievements(player.id)
    if "_seeded" not in known:
        storage.add_achievements(player.id, {code: detail for code, (_, detail) in earned.items()}, now,
                                 {code: ts for code, (ts, _) in earned.items()})
        storage.add_achievements(player.id, {"_seeded": None}, now)
        return []
    new = {code: detail for code, (_, detail) in earned.items() if code not in known}
    if new:
        storage.add_achievements(player.id, new, now, {code: earned[code][0] for code in new})
    return list(new.items())


def list_achievements(storage: Storage, chat_id: int, now: int, name: Optional[str] = None) -> list[tuple]:
    """[(имя, {code: (ts, деталь)})] по игрокам чата (или одному): заработанное сейчас, с датой из БД."""
    players = storage.list_players(chat_id)
    if name is not None:
        player = storage.get_player(chat_id, name)
        players = [player] if player else []
    result = []
    for player in players:
        earned = achievements.evaluate_timed(storage.get_matches(player.id))
        result.append((player.display_name, dict(earned)))  # дата — матч, на котором порог достигнут
    return result


def shared_games_since(storage: Storage, chat_id: int, since_ts: int) -> dict:
    """Совместные игры пати (≥2 игрока на одной стороне) начиная с since_ts: games/wins/losses."""
    named = [(p.display_name, storage.get_match_sides(p.id, since_ts=since_ts)) for p in storage.list_players(chat_id)]
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
    cooldown = GAME_REFRESH_COOLDOWN
    if hasattr(client, "api_key") and not client.api_key:
        # Без ключа лимит запросов OpenDota мал: пока пати не играет, опрашиваем реже —
        # иначе лимит кончается, и данные перестают обновляться совсем.
        last = storage.last_activity(chat.chat_id)
        if last is not None and now - last >= ACTIVE_WINDOW:
            cooldown = GAME_IDLE_COOLDOWN
    for player in storage.list_players(chat.chat_id):
        stale = player.updated_ts is None or (now - player.updated_ts) >= cooldown
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
                "average_rank": match.get("average_rank"),
            })
            entry["rows"].append({
                "name": player.display_name, "hero_id": match.get("hero_id"),
                "kills": match.get("kills") or 0, "deaths": match.get("deaths") or 0,
                "assists": match.get("assists") or 0,
                "won": stats.is_win(match["player_slot"], match["radiant_win"]),
                "step": chat.mmr_step, "current_mmr": summary.current_mmr,
                "streak_type": summary.streak_type, "streak_len": summary.streak_len,
                "gpm": match.get("gpm"), "hero_damage": match.get("hero_damage"), "position": match.get("position"),
                "imp": match.get("imp"), "leaver_status": match.get("leaver_status"),
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


def detect_presence(storage: Storage, steam, now: int) -> list[dict]:
    """Кто из игроков зашёл в Dota 2 (по Steam): события «start» по чатам, одним на всех зашедших за опрос.

    Статус запрашивается одним батчем на все аккаунты. Игроки с закрытым статусом пропускаются.
    Состояние (ingame_since/ingame_misses) хранится у игрока; конец сессии — после MISS_LIMIT опросов без Dota.
    """
    chats = [c for c in storage.list_chats() if c.notify_start]
    players_by_chat = {c.chat_id: storage.list_players(c.chat_id) for c in chats}
    ids = sorted({p.account_id for players in players_by_chat.values() for p in players})
    if not ids:
        return []
    states = steam.get_in_dota(ids)
    events: list[dict] = []
    for chat in chats:
        players = players_by_chat[chat.chat_id]
        started: list[str] = []
        in_game = 0
        for player in players:
            since, misses, event = advance(player.ingame_since, player.ingame_misses, states.get(player.account_id), now)
            if (since, misses) != (player.ingame_since, player.ingame_misses):
                storage.set_player_presence(player.id, since, misses)
            if event == "start":
                started.append(player.display_name)
            if since is not None:
                in_game += 1
        if started:
            events.append({"kind": "start", "chat_id": chat.chat_id, "names": started,
                           "in_game": in_game, "total": len(players)})
    return events


def build_records(storage: Storage, chat_id: int, since_ts: Optional[int]) -> dict:
    """Рекорды пати за период (из кэша БД, вся сохранённая история); since_ts=None — за всё время."""
    named = [(p.display_name, storage.get_matches(p.id, since_ts=since_ts)) for p in storage.list_players(chat_id)]
    return records.compute_records(named)


def build_period_awards(storage: Storage, chat_id: int, since_ts: int, min_games: int = 3) -> list[dict]:
    """Отличия участников за период (матчи с since_ts, из кэша БД). Без ≥2 участников сравнивать не с кем."""
    players = storage.list_players(chat_id)
    if len(players) < 2:
        return []
    chat = storage.get_or_create_chat(chat_id)
    named = [(p.display_name, storage.get_matches(p.id, since_ts=since_ts)) for p in players]
    return compute_period_awards(named, chat.mmr_step, min_games)


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
        "awards": build_period_awards(storage, chat_id, since),
    }


def build_mmr_series(storage: Storage, chat_id: int, since_ts: Optional[int]) -> dict[str, list[tuple[int, int]]]:
    """Динамика оценки MMR (накопленное ±) по игрокам чата: {имя: [(время, Δ)]}; без игр — игрок пропущен.

    since_ts — граница периода (игры берутся из всей сохранённой истории); None («всё») — вся история.
    """
    chat = storage.get_or_create_chat(chat_id)
    result: dict[str, list[tuple[int, int]]] = {}
    for player in storage.list_players(chat_id):
        start = since_ts if since_ts is not None else 0
        points = stats.mmr_series(storage.get_outcomes(player.id, since_ts=start), chat.mmr_step)
        if points:
            result[player.display_name] = points
    return result


def _fetch_matches(storage: Storage, client: OpenDotaClient, player: Player, now: int) -> tuple[list[dict], bool]:
    """Сырые матчи для сверки и признак «сверяли глубоко».

    Первая загрузка — вся ранкед-история. Дальше — лёгкий список последних матчей (один запрос,
    сразу с GPM/уроном), если он достаёт до нашего последнего матча. Иначе, и раз в DEEP_SYNC_SEC
    для самопроверки, — 200 ранкед-матчей; если и они все новые (бот долго стоял) — вся история.
    """
    if not storage.has_matches(player.id):
        return client.get_matches(player.account_id, limit=HISTORY_LIMIT_FIRST), True
    last = storage.latest_match_time(player.id)
    recent_fn = getattr(client, "get_recent_matches", None)
    deep_due = player.history_ts is None or now - player.history_ts >= DEEP_SYNC_SEC
    if recent_fn is not None and not deep_due:
        recent = recent_fn(player.account_id)
        times = [m["start_time"] for m in recent if m.get("start_time") is not None]
        if times and last is not None and min(times) <= last:  # выдача перекрывает сохранённое — пропусков нет
            return recent, False
    raw = client.get_matches(player.account_id, limit=HISTORY_LIMIT_REFRESH)
    times = [m["start_time"] for m in raw if m.get("start_time") is not None]
    if len(raw) >= HISTORY_LIMIT_REFRESH and last is not None and min(times, default=0) > last:
        raw = client.get_matches(player.account_id, limit=HISTORY_LIMIT_FIRST)  # разрыв больше 200 игр
    return raw, True


def refresh_player(
    storage: Storage, client: OpenDotaClient, player: Player, now: int,
    stratz: Optional[StratzClient] = None, enrich_cap: int = ENRICH_CAP, fast: bool = False,
) -> int:
    """Подтянуть ранкед-историю и текущий ранг. Вернуть число новых матчей.

    Матчи — главное: сбой профиля или доп. статистики их не блокирует, а «обновлён» (updated_ts)
    ставится только после успешной сверки матчей — иначе кулдаун выдавал бы устаревшие данные за свежие.

    fast=True — путь команды пользователя: у OpenDota берём только матчи (и ранг, если были новые
    игры) — один-два запроса на игрока; позиции Stratz — одной пачкой. Медленное (пинок OpenDota,
    средние/линии, детали матчей) помечается и доделывается в finish_refresh фоном, уже после ответа.
    """
    if not fast:
        # Пнуть OpenDota перечитать историю — свежие игры доедут быстрее (best-effort).
        try:
            client.refresh(player.account_id)
        except Exception:
            pass

    # Храним ВСЮ ранкед-историю (для героев/позиций/матчей за любой период). Окно «с момента
    # добавления» применяется только в лидерборде (build_player_summary).
    raw_matches, deep = _fetch_matches(storage, client, player, now)
    fresh = [
        _normalize(m)
        for m in raw_matches
        if stats.is_ranked_lobby(m.get("lobby_type"))
        and m.get("radiant_win") is not None  # исход неизвестен → не считаем матч
    ]
    inserted = storage.add_matches(player.id, fresh)
    storage.touch_player(player.id, now, deep=deep)

    # Ранг меняется только после игр: без новых матчей перечитываем профиль изредка
    # (его же раз в полчаса обновляет проверка Steam-профилей).
    if inserted or player.profile_ts is None or now - player.profile_ts >= PROFILE_TTL:
        try:
            profile = client.get_profile(player.account_id)
            if profile is not None:
                _store_profile(storage, player, profile, now)
        except Exception:
            log.debug("Не удалось получить профиль игрока %s", player.account_id, exc_info=True)

    if not fast and enrich_cap > 0:
        _enrich_from_opendota(storage, client, player, enrich_cap)

    if stratz is not None:
        _enrich_from_stratz(storage, stratz, player, now)

    return inserted


def _counts_for_mmr(match: dict, anchor_ts: int) -> bool:
    """Матч влияет на оценку MMR, если закончился после якоря.

    MMR, который игрок ввёл в момент якоря, уже включает все завершённые игры; игра, шедшая
    в этот момент (началась раньше), ещё не включена — её считаем. Без длительности — по началу.
    """
    duration = match.get("duration")
    if duration:
        return match["start_time"] + duration > anchor_ts
    return match["start_time"] >= anchor_ts


def build_player_summary(storage: Storage, chat: Chat, player: Player, now: int) -> PlayerSummary:
    step = chat.mmr_step

    all_matches = storage.get_matches(player.id)  # вся ранкед-история; MMR-оценка — от якоря ниже
    # Матчи отсортированы по start_time: окна — срезы той же выборки, без лишних запросов к БД.
    anchor_matches = [m for m in all_matches if _counts_for_mmr(m, player.anchor_ts)]
    day_start = stats.local_day_start(now, chat.tz)
    today_matches = [m for m in all_matches if m["start_time"] >= day_start]

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
    lobby_rank = median_rank_tier(lobby_ranks)
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
        party_unknown=split["unknown"],
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
        detail_games=sum(1 for m in all_matches if m.get("gpm") is not None),
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
        history_closed=player.fh_unavailable,
        anchor_games=agg_anchor.games,
        mmr_drift=mmr_rank_mismatch(current_mmr, player.last_rank_tier),
    )


def refresh_chat(
    storage: Storage, client: OpenDotaClient, chat_id: int, now: int,
    stratz: Optional[StratzClient] = None, players: Optional[list[Player]] = None,
    fast: bool = False,
) -> list[Player]:
    """Обновить устаревших игроков чата (параллельно) и вернуть актуальный список игроков.

    Без сборки сводок — этого достаточно, когда нужны только свежие матчи (например, для графика).
    fast=True — матчи, ранг и позиции Stratz (быстрый ответ на команду); остальное догоняет finish_refresh.
    """
    players = players if players is not None else storage.list_players(chat_id)
    # Кулдаун: если обновляли недавно — берём кэш из БД, не дёргаем OpenDota (скорость).
    stale = [p for p in players if p.updated_ts is None or (now - p.updated_ts) >= REFRESH_COOLDOWN]
    if not stale:
        return players

    def _refresh_safe(player: Player) -> None:
        try:
            refresh_player(storage, client, player, now, stratz=stratz, fast=fast)
        except Exception:  # ошибка по одному игроку не должна рушить весь чат
            log.warning("Не удалось обновить игрока %s (id %s), беру кэш",
                        player.display_name, player.account_id, exc_info=True)

    if len(stale) > 1:  # игроки независимы: сетевые ожидания перекрываются
        with ThreadPoolExecutor(max_workers=min(REFRESH_WORKERS, len(stale))) as pool:
            list(pool.map(_refresh_safe, stale))
    else:
        _refresh_safe(stale[0])
    fresh = {p.account_id: p for p in storage.list_players(chat_id)}  # точная перечитка по account_id
    return [fresh.get(p.account_id, p) for p in players]


def build_leaderboard(
    storage: Storage,
    client: OpenDotaClient,
    chat_id: int,
    now: int,
    refresh: bool = True,
    stratz: Optional[StratzClient] = None,
    fast: bool = False,
) -> list[PlayerSummary]:
    chat = storage.get_or_create_chat(chat_id)
    players = storage.list_players(chat_id)

    if refresh:
        players = refresh_chat(storage, client, chat_id, now, stratz, players, fast)

    summaries = [build_player_summary(storage, chat, player, now) for player in players]

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
    if name:
        target = storage.get_player(chat_id, name)
        if target is None:
            return None
        candidates = [target]
    else:
        candidates = storage.list_players(chat_id)

    by_id = {player.id: player for player in candidates}
    row = storage.get_latest_match(list(by_id), match_id)  # один запрос вместо выгрузки всей истории
    if row is None:
        return None
    return {"player": by_id[row["player_id"]], "match": row}


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


def median_rank_tier(tiers: list[int]) -> Optional[int]:
    """Медиана rank_tier, всегда существующее значение набора (среднее 45 и 52 дало бы невозможный «Archon 8»)."""
    return statistics.median_low(tiers) if tiers else None


def _best_worst_hour(by_hour: dict, min_games: int = 3):
    """Из {час: (игр, побед)} выбрать лучший/худший час (по винрейту, порог по играм).

    Нужны минимум два часа с разным винрейтом: иначе один и тот же час был бы и лучшим, и худшим.
    """
    qualified = [(hour, wins / games, games) for hour, (games, wins) in by_hour.items() if games >= min_games]
    if len(qualified) < 2:
        return (None, None)
    best = max(qualified, key=lambda x: x[1])
    worst = min(qualified, key=lambda x: x[1])
    if best[1] == worst[1]:
        return (None, None)
    return ((best[0], best[1]), (worst[0], worst[1]))


# Метрики для сравнения игроков внутри чата (все — «выше = лучше»).
MIN_COMPARE_GAMES = 5  # перф/GPM сравниваем, только если они посчитаны хотя бы по стольким играм (иначе это шум)

_COMPARE_METRICS = {
    "perf": lambda s: s.avg_perf if s.enriched_games >= MIN_COMPARE_GAMES else None,
    "winrate": lambda s: s.winrate if s.games_total else None,
    "kda": lambda s: s.kda_ratio if s.games_total else None,
    "gpm": lambda s: s.avg_gpm_window if s.detail_games >= MIN_COMPARE_GAMES else None,
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
    named_matches = [(p.display_name, storage.get_match_sides(p.id)) for p in players]  # стороны и исходы — этого достаточно
    return {
        "player_count": len(players),
        "summary": party.together_summary(named_matches),
        "duo": party.best_duo(named_matches),
    }
