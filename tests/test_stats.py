import pytest

from mmrbot.stats import (
    aggregate,
    aggregate_skill,
    best_game,
    current_streak,
    duration_stats,
    estimate_mmr_delta,
    hero_pool,
    infer_role_style,
    is_ranked_lobby,
    is_win,
    longest_win_streak,
    perf_score,
    recent_form,
    solo_party_split,
    top_heroes,
    winrate_by_hour,
    wins_losses_split,
)


def make(slot, radiant_win, k=0, d=0, a=0):
    return {"player_slot": slot, "radiant_win": radiant_win, "kills": k, "deaths": d, "assists": a}


# --- is_win -------------------------------------------------------------

def test_radiant_player_wins_when_radiant_wins():
    assert is_win(player_slot=0, radiant_win=True) is True


def test_radiant_player_loses_when_dire_wins():
    assert is_win(player_slot=4, radiant_win=False) is False


def test_dire_player_wins_when_dire_wins():
    assert is_win(player_slot=128, radiant_win=False) is True


def test_dire_player_loses_when_radiant_wins():
    assert is_win(player_slot=132, radiant_win=True) is False


# --- is_ranked_lobby ----------------------------------------------------

def test_lobby_7_is_ranked():
    assert is_ranked_lobby(7) is True


def test_lobby_0_is_not_ranked():
    assert is_ranked_lobby(0) is False


def test_lobby_none_is_not_ranked():
    assert is_ranked_lobby(None) is False


# --- aggregate ----------------------------------------------------------

def test_aggregate_empty_is_all_zero_without_crash():
    agg = aggregate([])
    assert agg.games == 0
    assert agg.wins == 0
    assert agg.losses == 0
    assert agg.winrate == 0.0
    assert agg.avg_kills == 0.0
    assert agg.kda_ratio == 0.0


def test_aggregate_counts_wins_losses_and_kda():
    matches = [
        make(0, True, k=5, d=2, a=10),    # radiant win
        make(128, False, k=3, d=5, a=7),  # dire win
        make(1, False, k=1, d=8, a=2),    # radiant loss
    ]
    agg = aggregate(matches)
    assert agg.games == 3
    assert agg.wins == 2
    assert agg.losses == 1
    assert agg.winrate == pytest.approx(2 / 3)
    assert agg.avg_kills == pytest.approx(3.0)
    assert agg.avg_deaths == pytest.approx(5.0)
    assert agg.avg_assists == pytest.approx(19 / 3)
    assert agg.kda_ratio == pytest.approx((5 + 3 + 1 + 10 + 7 + 2) / (2 + 5 + 8))


def test_aggregate_kda_guards_zero_deaths():
    matches = [make(0, True, k=10, d=0, a=5)]
    agg = aggregate(matches)
    assert agg.kda_ratio == pytest.approx(15.0)  # (10+5)/max(0,1)


# --- estimate_mmr_delta -------------------------------------------------

def test_mmr_delta_positive():
    assert estimate_mmr_delta(wins=6, losses=4, step=25) == 50


def test_mmr_delta_negative():
    assert estimate_mmr_delta(wins=3, losses=7, step=25) == -100


def test_mmr_delta_custom_step():
    assert estimate_mmr_delta(wins=5, losses=0, step=30) == 150


def test_mmr_delta_zero_when_even():
    assert estimate_mmr_delta(wins=4, losses=4, step=25) == 0


# --- current_streak (матчи по возрастанию времени) ----------------------

def test_streak_empty():
    assert current_streak([]) == ("", 0)


def test_streak_single_win():
    assert current_streak([make(0, True)]) == ("W", 1)


def test_streak_breaks_on_last_result():
    # ...W, W, L  → текущая серия: 1 поражение
    assert current_streak([make(0, True), make(0, True), make(0, False)]) == ("L", 1)


def test_streak_counts_tail_wins():
    # L, W, W, W → 3 победы подряд
    matches = [make(0, False), make(0, True), make(128, False), make(0, True)]
    assert current_streak(matches) == ("W", 3)


# --- top_heroes ---------------------------------------------------------

def hmatch(hero_id, slot, radiant_win):
    m = make(slot, radiant_win)
    m["hero_id"] = hero_id
    return m


def test_top_heroes_empty():
    assert top_heroes([]) == []


def test_top_heroes_counts_and_sorts():
    matches = [
        hmatch(1, 0, True), hmatch(1, 0, False),   # hero 1: 2 игры, 1 победа
        hmatch(2, 0, True), hmatch(2, 128, False),  # hero 2: 2 игры, 2 победы
        hmatch(3, 0, False),                        # hero 3: 1 игра, 0 побед
    ]
    top = top_heroes(matches, k=3)
    assert [h["hero_id"] for h in top] == [2, 1, 3]  # по играм, тай-брейк по винрейту
    assert top[0]["games"] == 2 and top[0]["wins"] == 2
    assert top[1]["winrate"] == pytest.approx(0.5)


# --- winrate_by_hour ----------------------------------------------------

def test_winrate_by_hour_buckets_local_time():
    # 1790000000 → в МСК определённый час; проверяем, что бакетизация по локальному часу работает
    m1 = make(0, True)
    m1["start_time"] = 1790000000
    m2 = make(0, False)
    m2["start_time"] = 1790000000 + 3600  # +1 час
    by_hour = winrate_by_hour([m1, m2], "Europe/Moscow")
    total_games = sum(g for g, w in by_hour.values())
    assert total_games == 2
    assert len(by_hour) == 2  # два разных часа


# --- solo_party_split ---------------------------------------------------

def pmatch(party_size, slot, radiant_win):
    m = make(slot, radiant_win)
    m["party_size"] = party_size
    return m


def test_solo_party_split():
    matches = [
        pmatch(1, 0, True),    # solo win
        pmatch(1, 0, False),   # solo loss
        pmatch(3, 0, True),    # party win
    ]
    split = solo_party_split(matches)
    assert split["solo"] == (2, 1)   # (games, wins)
    assert split["party"] == (1, 1)


def test_solo_party_missing_party_size_is_unknown_not_solo():
    matches = [make(0, True), pmatch(1, 0, False)]  # у первой размер пати неизвестен
    split = solo_party_split(matches)
    assert split["unknown"] == (1, 1)
    assert split["solo"] == (1, 0)


# --- duration_stats -----------------------------------------------------

def test_duration_stats_empty():
    d = duration_stats([])
    assert d["avg_minutes"] == 0
    assert d["max_minutes"] == 0


def test_duration_stats_avg_and_max():
    matches = [{"duration": 1800}, {"duration": 3600}]  # 30 и 60 минут
    d = duration_stats(matches)
    assert d["avg_minutes"] == pytest.approx(45.0)
    assert d["max_minutes"] == pytest.approx(60.0)


# --- recent_form / best_game / longest_win_streak -----------------------

def test_recent_form_last_n_in_order():
    # W L W W L W  (6 матчей)
    matches = [make(0, True), make(0, False), make(0, True), make(0, True), make(0, False), make(0, True)]
    form = recent_form(matches, n=5)
    assert form == [False, True, True, False, True]  # последние 5 по порядку


def test_recent_form_fewer_than_n():
    assert recent_form([make(0, True), make(0, False)], n=5) == [True, False]


def test_recent_form_empty():
    assert recent_form([], n=5) == []


def test_best_game_max_kda():
    matches = [
        make(0, True, k=5, d=2, a=5),    # KDA 5.0
        make(0, True, k=10, d=1, a=10),  # KDA 20.0 ← лучший
        make(0, False, k=1, d=9, a=1),   # KDA ~0.22
    ]
    best = best_game(matches)
    assert best["kills"] == 10 and best["deaths"] == 1 and best["assists"] == 10


def test_best_game_empty():
    assert best_game([]) is None


def test_longest_win_streak():
    # W W L W W W L
    matches = [make(0, True), make(0, True), make(0, False), make(0, True), make(0, True), make(0, True), make(0, False)]
    assert longest_win_streak(matches) == 3


def test_longest_win_streak_none():
    assert longest_win_streak([make(0, False), make(0, False)]) == 0
    assert longest_win_streak([]) == 0


# --- perf_score (role-normalized) ---------------------------------------

def test_perf_score_averages_positive_benchmarks():
    # deaths_per_min исключаем (высокий = плохо), остальное усредняем
    bench = {"gold_per_min": 0.5, "xp_per_min": 0.7, "hero_healing_per_min": 0.9, "deaths_per_min": 0.95}
    assert perf_score(bench) == pytest.approx((0.5 + 0.7 + 0.9) / 3)


def test_perf_score_support_beats_core_when_role_appropriate():
    # Саппорт: высокие хил/ассисты, низкий фарм → перцентили высоки на своих осях
    support = {"hero_healing_per_min": 0.9, "assists_per_min": 0.85, "gold_per_min": 0.2, "last_hits_per_min": 0.15}
    core = {"gold_per_min": 0.4, "last_hits_per_min": 0.45, "hero_damage_per_min": 0.4, "assists_per_min": 0.3}
    assert perf_score(support) > perf_score(core)


def test_perf_score_empty_is_none():
    assert perf_score({}) is None
    assert perf_score({"deaths_per_min": 0.9}) is None  # только негативная метрика


# --- aggregate_skill / infer_role_style ---------------------------------

def test_aggregate_skill_averages_per_metric():
    benches = [
        {"gold_per_min": 0.4, "hero_damage_per_min": 0.8},
        {"gold_per_min": 0.6, "kills_per_min": 0.5},
    ]
    skill = aggregate_skill(benches)
    assert skill["gold_per_min"] == pytest.approx(0.5)
    assert skill["hero_damage_per_min"] == pytest.approx(0.8)
    assert skill["kills_per_min"] == pytest.approx(0.5)


def test_aggregate_skill_empty():
    assert aggregate_skill([]) == {}


def test_infer_role_core_by_farm():
    assert infer_role_style(avg_last_hits=220, avg_hero_healing=200) == "кор (фарм)"


def test_infer_role_support_by_healing():
    assert "саппорт" in infer_role_style(avg_last_hits=120, avg_hero_healing=6000)


def test_infer_role_support_by_low_farm():
    assert "саппорт" in infer_role_style(avg_last_hits=50, avg_hero_healing=500)


def test_infer_role_unknown_without_data():
    assert infer_role_style(avg_last_hits=None, avg_hero_healing=None) == ""


# --- wins_losses_split / hero_pool --------------------------------------

def test_wins_losses_split():
    matches = [
        make(0, True, k=8, d=2, a=6),    # win, мало смертей
        make(0, True, k=6, d=3, a=8),    # win
        make(0, False, k=1, d=10, a=2),  # loss, много смертей
    ]
    split = wins_losses_split(matches)
    assert split["win"]["games"] == 2
    assert split["loss"]["games"] == 1
    assert split["win"]["avg_deaths"] == pytest.approx(2.5)
    assert split["loss"]["avg_deaths"] == pytest.approx(10.0)


def test_wins_losses_split_no_losses():
    split = wins_losses_split([make(0, True, k=1, d=1, a=1)])
    assert split["win"]["games"] == 1
    assert split["loss"] is None


def test_hero_pool_counts_distinct():
    matches = [
        {"hero_id": 1}, {"hero_id": 1}, {"hero_id": 2}, {"hero_id": 3}, {"hero_id": None},
    ]
    assert hero_pool(matches) == 3


def test_hero_pool_empty():
    assert hero_pool([]) == 0


# --- hero_stats / role_stats -------------------------------------------

def _m(match_id, start, hero, slot=0, rw=True, k=1, d=1, a=1, imp=None, position=None, gpm=None):
    return {"match_id": match_id, "start_time": start, "hero_id": hero, "player_slot": slot,
            "radiant_win": rw, "kills": k, "deaths": d, "assists": a, "imp": imp,
            "position": position, "gpm": gpm}


def test_hero_stats_aggregates_and_sorts():
    from mmrbot.stats import hero_stats
    ms = [
        _m(1, 100, 1, k=10, d=2, a=4, imp=20, gpm=600),
        _m(2, 200, 1, rw=False, k=2, d=6, a=2, imp=-10, gpm=400),
        _m(3, 300, 2),
    ]
    res = hero_stats(ms)
    assert [h["hero_id"] for h in res] == [1, 2]
    top = res[0]
    assert (top["games"], top["wins"]) == (2, 1)
    assert top["winrate"] == 0.5
    assert top["kda"] == (10 + 2 + 4 + 2) / (2 + 6)
    assert top["avg_imp"] == 5
    assert top["avg_gpm"] == 500
    assert res[1]["avg_imp"] is None


def test_hero_stats_since_filters_period():
    from mmrbot.stats import hero_stats
    ms = [_m(1, 100, 1), _m(2, 500, 2)]
    assert [h["hero_id"] for h in hero_stats(ms, since_ts=300)] == [2]


def test_hero_stats_limit_and_single_hero():
    from mmrbot.stats import hero_stats
    ms = [_m(i, i, i % 3 + 1) for i in range(9)]
    assert len(hero_stats(ms, limit=2)) == 2
    assert [h["hero_id"] for h in hero_stats(ms, hero_id=2)] == [2]


def test_role_stats_by_position():
    from mmrbot.stats import role_stats
    ms = [_m(1, 1, 1, position=1, imp=10), _m(2, 2, 1, rw=False, position=1, imp=0),
          _m(3, 3, 1, position=5), _m(4, 4, 1)]
    res = role_stats(ms)
    assert [r["position"] for r in res] == [1, 5]
    assert res[0]["games"] == 2 and res[0]["winrate"] == 0.5 and res[0]["avg_imp"] == 5


def test_period_since():
    from mmrbot.stats import period_since
    assert period_since("day", 1_000_000) == 1_000_000 - 86_400
    assert period_since("week", 1_000_000) == 1_000_000 - 7 * 86_400
    assert period_since("month", 1_000_000) == 1_000_000 - 30 * 86_400
    assert period_since("all", 1_000_000) is None


def test_local_day_start_moscow():
    from datetime import datetime, timezone
    from mmrbot.stats import local_day_start
    # 2026-10-01 15:00 МСК (UTC+3) = 12:00 UTC; полночь МСК = 2026-09-30 21:00 UTC
    now = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    expected = int(datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc).timestamp())
    assert local_day_start(now, "Europe/Moscow") == expected


def test_local_day_start_just_after_midnight():
    from datetime import datetime, timezone
    from mmrbot.stats import local_day_start
    # 00:30 МСК = 21:30 UTC накануне → начало суток ровно 30 минут назад
    now = int(datetime(2026, 9, 30, 21, 30, tzinfo=timezone.utc).timestamp())
    assert local_day_start(now, "Europe/Moscow") == now - 1800


def test_local_day_start_bad_tz_falls_back_to_moscow():
    from datetime import datetime, timezone
    from mmrbot.stats import local_day_start
    now = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    assert local_day_start(now, "Nope/Zone") == local_day_start(now, "Europe/Moscow")


# --- аудит данных: точность ---------------------------------------------

def test_perf_score_counts_tower_damage_benchmark():
    """OpenDota отдаёт benchmark `tower_damage` (не `..._per_min`) — он должен входить в perf."""
    from mmrbot.stats import perf_score
    assert perf_score({"gold_per_min": 0.5, "tower_damage": 0.9}) == pytest.approx(0.7)


def test_best_game_ignores_low_activity_games():
    """«0 смертей, 2 помощи» (KDA 2) не должен быть лучшей игрой, когда есть настоящая."""
    matches = [make(0, True, k=0, d=0, a=9), make(0, True, k=8, d=4, a=8)]
    best = best_game(matches)
    assert (best["kills"], best["assists"]) == (8, 8)


def test_best_game_none_when_no_game_reaches_threshold():
    assert best_game([make(0, True, k=1, d=0, a=1)]) is None
