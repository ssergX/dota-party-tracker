import pytest

from mmrbot.storage import Storage
from mmrbot.tracker import (
    build_chat_comparison,
    build_leaderboard,
    build_player_summary,
    build_together,
    refresh_player,
)


class FakeOpenDota:
    """Фейковый клиент: без сети, возвращает заранее заданные профиль и матчи."""

    def __init__(self, profile=None, matches=None, match_stats=None):
        self.profile = profile or {"rank_tier": None, "leaderboard_rank": None, "personaname": None}
        self.matches = matches or []
        self.match_stats = match_stats  # dict пер-матч статы (одинаковые для всех) или None
        self.profile_calls = 0
        self.match_calls = 0
        self.refresh_calls = 0

    def refresh(self, account_id):
        self.refresh_calls += 1
        return True

    def get_match_player_stats(self, match_id, account_id, player_slot=None):
        return self.match_stats

    def get_profile(self, account_id):
        self.profile_calls += 1
        return self.profile

    def get_matches(self, account_id, limit=200):
        self.match_calls += 1
        return list(self.matches)


def od_match(match_id, start_time, slot=0, radiant_win=True, lobby_type=7, k=1, d=1, a=1, hero_id=1):
    return {
        "match_id": match_id,
        "start_time": start_time,
        "player_slot": slot,
        "radiant_win": radiant_win,
        "lobby_type": lobby_type,
        "kills": k,
        "deaths": d,
        "assists": a,
        "hero_id": hero_id,
    }


@pytest.fixture
def store(tmp_path):
    return Storage(str(tmp_path / "t.db"))


# --- refresh_player -----------------------------------------------------

def test_refresh_stores_all_ranked_history(store):
    player = store.add_player(100, 42, "Вася", 5000, anchor_ts=1000, created_ts=1000)
    client = FakeOpenDota(
        profile={"rank_tier": 75, "leaderboard_rank": None, "personaname": "Вася"},
        matches=[
            od_match(1, start_time=500, lobby_type=7),    # до добавления игрока — тоже история
            od_match(2, start_time=1500, lobby_type=0),   # не ранкед — игнор
            od_match(3, start_time=1500, lobby_type=7),   # ок
            od_match(4, start_time=2500, lobby_type=7),   # ок
        ],
    )
    new_count = refresh_player(store, client, player, now=3000)
    assert new_count == 3
    stored = store.get_matches(player.id)
    assert {m["match_id"] for m in stored} == {1, 3, 4}


def test_summary_counts_whole_history_but_mmr_only_since_anchor(store):
    player = store.add_player(100, 42, "Вася", 5000, anchor_ts=1000, created_ts=1000)
    client = FakeOpenDota(matches=[od_match(1, 500), od_match(2, 1500), od_match(3, 2500, radiant_win=False)])
    refresh_player(store, client, player, now=3000)
    summary = build_player_summary(store, store.get_or_create_chat(100), player, now=3000)
    assert summary.games_total == 3                      # статистика — по всей истории
    assert summary.mmr_delta == 0                        # MMR-оценка: с якоря — 1 победа и 1 поражение
    assert len(store.get_matches(player.id)) == 3


def test_first_refresh_loads_deep_history_then_recent_only(store):
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    limits = []

    class Spy(FakeOpenDota):
        def get_matches(self, account_id, limit=200):
            limits.append(limit)
            return super().get_matches(account_id, limit)

    client = Spy(matches=[od_match(1, 1500)])
    refresh_player(store, client, player, now=3000)
    refresh_player(store, client, player, now=3100)
    assert limits[0] is None and limits[1] == 200     # первая загрузка — вся история, дальше свежие


def test_refresh_updates_rank(store):
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    client = FakeOpenDota(profile={"rank_tier": 63, "leaderboard_rank": None, "personaname": "Вася"})
    refresh_player(store, client, player, now=3000)
    refreshed = store.get_player(100, "Вася")
    assert refreshed.last_rank_tier == 63
    assert refreshed.updated_ts == 3000


def test_refresh_is_idempotent(store):
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    client = FakeOpenDota(matches=[od_match(3, 1500), od_match(4, 2500)])
    assert refresh_player(store, client, player, now=3000) == 2
    assert refresh_player(store, client, player, now=4000) == 0  # те же матчи — 0 новых


def test_refresh_skips_matches_with_unknown_outcome(store):
    # radiant_win=None (матч ещё не финализирован) не должен сохраняться как поражение.
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    client = FakeOpenDota(matches=[
        od_match(3, 1500, radiant_win=None),  # исход неизвестен — пропустить
        od_match(4, 2500, radiant_win=True),  # ок
    ])
    inserted = refresh_player(store, client, player, now=3000)
    assert inserted == 1
    assert {m["match_id"] for m in store.get_matches(player.id)} == {4}


def test_leaderboard_survives_one_player_refresh_error(store):
    now = 100_000
    good = store.add_player(100, 1, "Good", 5000, 1000, 1000)
    store.add_player(100, 2, "Bad", 4000, 1000, 1000)
    store.add_matches(good.id, [{"match_id": 10, "start_time": 2000, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 1, "deaths": 1, "assists": 1, "hero_id": 1}])

    class FlakyClient(FakeOpenDota):
        def get_profile(self, account_id):
            if account_id == 2:
                raise RuntimeError("OpenDota HTTP 503")
            return super().get_profile(account_id)

    board = build_leaderboard(store, FlakyClient(), 100, now=now, refresh=True)
    names = {s.display_name for s in board}
    assert names == {"Good", "Bad"}  # битый игрок не рушит весь лидерборд


def _m(match_id, start_time, slot=0, radiant_win=True, hero_id=1, k=1, d=1, a=1, lobby_type=7, duration=1800, party_size=1):
    return {
        "match_id": match_id, "start_time": start_time, "player_slot": slot,
        "radiant_win": radiant_win, "lobby_type": lobby_type, "kills": k, "deaths": d,
        "assists": a, "hero_id": hero_id, "duration": duration, "party_size": party_size,
    }


def test_summary_includes_streak_and_top_heroes(store):
    now = 100_000
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [
        _m(1, 2000, hero_id=1, radiant_win=False),  # loss
        _m(2, 3000, hero_id=1, radiant_win=True),   # win
        _m(3, 4000, hero_id=2, radiant_win=True),   # win
    ])
    p = store.get_player(100, "Вася")
    chat = store.get_or_create_chat(100)
    s = build_player_summary(store, chat, p, now=now)
    assert (s.streak_type, s.streak_len) == ("W", 2)
    assert s.top_heroes[0]["hero_id"] == 1  # 2 игры на герое 1


def test_refresh_enriches_matches_and_perf(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    client = FakeOpenDota(
        matches=[od_match(1, 1500), od_match(2, 2500)],
        match_stats={
            "gpm": 500, "xpm": 600, "last_hits": 180, "denies": 10, "hero_damage": 25000,
            "tower_damage": 3000, "hero_healing": 0, "net_worth": 18000, "level": 25,
            "benchmarks": {"gold_per_min": 0.6, "hero_damage_per_min": 0.8},
        },
    )
    refresh_player(store, client, p, now=3000)
    rows = store.get_matches(p.id)
    assert all(r["enriched"] == 1 for r in rows)
    assert rows[0]["gpm"] == 500 and rows[0]["net_worth"] == 18000

    p2 = store.get_player(100, "Вася")
    chat = store.get_or_create_chat(100)
    s = build_player_summary(store, chat, p2, now=100_000)
    assert s.avg_perf == pytest.approx((0.6 + 0.8) / 2)
    assert s.enriched_games == 2
    assert s.avg_gpm_window == pytest.approx(500)
    # профиль скилла (перцентили) и роль
    assert s.skill["gold_per_min"] == pytest.approx(0.6)
    assert s.skill["hero_damage_per_min"] == pytest.approx(0.8)
    assert s.role_style == "кор (фарм)"  # last_hits 180 → кор


def test_build_chat_comparison_ranks_and_power(store):
    from dataclasses import replace
    now = 100_000
    p = store.add_player(100, 1, "Base", 5000, 1000, 1000)
    store.add_matches(p.id, [_m(10 + i, 2000 + i, radiant_win=(i % 2 == 0)) for i in range(4)])
    base = build_leaderboard(store, FakeOpenDota(), 100, now=now, refresh=False)[0]
    # A лучше по всем метрикам, B хуже
    a = replace(base, display_name="A", avg_perf=0.8, winrate=0.6, kda_ratio=4.0, avg_gpm_window=500.0, games_total=5, enriched_games=5, detail_games=5)
    b = replace(base, display_name="B", avg_perf=0.4, winrate=0.4, kda_ratio=2.0, avg_gpm_window=400.0, games_total=5, enriched_games=5, detail_games=5)
    comp = build_chat_comparison([a, b])
    assert comp["size"] == 2
    assert comp["players"]["A"]["ranks"]["perf"] == 1
    assert comp["players"]["B"]["ranks"]["perf"] == 2
    assert comp["players"]["A"]["power_rank"] == 1
    assert comp["players"]["B"]["power_rank"] == 2
    assert "perf" in comp["players"]["A"]["leads"]


def test_build_chat_comparison_handles_missing_metrics(store):
    from dataclasses import replace
    now = 100_000
    p = store.add_player(100, 1, "Base", 5000, 1000, 1000)
    store.add_matches(p.id, [_m(10, 2000)])
    base = build_leaderboard(store, FakeOpenDota(), 100, now=now, refresh=False)[0]
    a = replace(base, display_name="A", avg_perf=0.7, games_total=5, enriched_games=5)
    b = replace(base, display_name="B", avg_perf=None, games_total=5)  # без перфа
    comp = build_chat_comparison([a, b])
    assert comp["players"]["A"]["ranks"]["perf"] == 1
    assert "perf" not in comp["players"]["B"]["ranks"]  # нет метрики — нет ранга




def test_summary_includes_form_lanes_and_records(store):
    now = 100_000
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.update_player_insights(p.id, lanes_json='{"2": [10, 6]}', gpm_median=500, gpm_best=800)
    store.add_matches(p.id, [
        _m(1, 2000, radiant_win=True),
        _m(2, 3000, radiant_win=True),
        _m(3, 4000, radiant_win=False),
        _m(4, 5000, radiant_win=True, k=8, d=2, a=8),
    ])
    p = store.get_player(100, "Вася")
    chat = store.get_or_create_chat(100)
    s = build_player_summary(store, chat, p, now=now)
    assert s.recent_form == [True, True, False, True]
    assert s.longest_win_streak == 2
    assert s.lanes[2] == (10, 6)
    assert s.gpm_median == 500
    assert s.best_game is not None


def test_summary_includes_solo_party_and_totals(store):
    now = 100_000
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.update_player_totals(p.id, gpm=500.0, xpm=600.0, last_hits=180.0)
    store.add_matches(p.id, [
        _m(1, 2000, party_size=1, radiant_win=True),   # solo win
        _m(2, 3000, party_size=3, radiant_win=False),  # party loss
    ])
    p = store.get_player(100, "Вася")
    chat = store.get_or_create_chat(100)
    s = build_player_summary(store, chat, p, now=now)
    assert s.gpm == 500.0
    assert s.solo == (1, 1)
    assert s.party == (1, 0)
    assert s.avg_duration_min > 0






def test_build_together_counts_shared(store):
    now = 100_000
    a = store.add_player(100, 1, "Alice", 5000, 1000, 1000)
    b = store.add_player(100, 2, "Bob", 4000, 1000, 1000)
    store.add_matches(a.id, [_m(1, 2000, radiant_win=True, party_size=2), _m(2, 3000, radiant_win=False)])
    store.add_matches(b.id, [_m(1, 2000, radiant_win=True, party_size=2), _m(9, 3000, radiant_win=True)])
    result = build_together(store, 100)
    assert result["summary"]["games"] == 1  # общий матч 1
    assert result["summary"]["wins"] == 1


def test_leaderboard_reread_by_account_id_not_name(store):
    # Игрок с именем-числом не должен подменять другого при внутренней перечитке.
    now = 100_000
    store.add_player(100, 555, "Alice", anchor_mmr=5000, anchor_ts=1000, created_ts=1000)
    store.add_player(100, 999, "555", anchor_mmr=4000, anchor_ts=1000, created_ts=1000)
    board = build_leaderboard(store, FakeOpenDota(), 100, now=now, refresh=True)
    by_name = {s.display_name: s.anchor_mmr for s in board}
    assert by_name["Alice"] == 5000
    assert by_name["555"] == 4000


# --- build_player_summary ----------------------------------------------

def test_summary_computes_mmr_and_windows(store):
    now = 100_000
    player = store.add_player(100, 42, "Вася", anchor_mmr=5000, anchor_ts=1000, created_ts=1000)
    store.add_matches(player.id, [
        {"match_id": 1, "start_time": 2000, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 10, "deaths": 2, "assists": 5, "hero_id": 1},    # win, старый
        {"match_id": 2, "start_time": 3000, "player_slot": 0, "radiant_win": False, "lobby_type": 7, "kills": 1, "deaths": 8, "assists": 2, "hero_id": 2},     # loss, старый
        {"match_id": 3, "start_time": 90000, "player_slot": 128, "radiant_win": False, "lobby_type": 7, "kills": 6, "deaths": 3, "assists": 9, "hero_id": 3},  # win, сегодня
        {"match_id": 4, "start_time": 95000, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 8, "deaths": 4, "assists": 7, "hero_id": 4},     # win, сегодня
    ])
    store.update_player_rank(player.id, rank_tier=75, leaderboard_rank=None, updated_ts=now)
    player = store.get_player(100, "Вася")
    chat = store.get_or_create_chat(100)  # step=25 default

    s = build_player_summary(store, chat, player, now=now)
    assert s.games_total == 4
    assert s.wins_total == 3
    assert s.losses_total == 1
    assert s.mmr_delta == 50            # (3-1)*25
    assert s.current_mmr == 5050        # 5000 + 50
    assert s.games_today == 2           # start_time >= now-86400 (13600)
    assert s.wins_today == 2
    assert s.delta_today == 50          # (2-0)*25
    assert s.rank == "Divine 5"


def test_summary_without_anchor_mmr_has_none_current(store):
    now = 100_000
    player = store.add_player(100, 42, "NoMMR", anchor_mmr=None, anchor_ts=1000, created_ts=1000)
    store.add_matches(player.id, [
        {"match_id": 1, "start_time": 2000, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 1, "deaths": 1, "assists": 1, "hero_id": 1},
    ])
    player = store.get_player(100, "NoMMR")
    chat = store.get_or_create_chat(100)
    s = build_player_summary(store, chat, player, now=now)
    assert s.current_mmr is None
    assert s.mmr_delta == 25  # дельта считается всё равно (1-0)*25


# --- build_leaderboard --------------------------------------------------

def test_leaderboard_skips_refresh_when_recent(store):
    now = 100_000
    p = store.add_player(100, 1, "A", 5000, 1000, 1000)
    store.update_player_rank(p.id, 80, None, updated_ts=now - 10)  # обновлён 10с назад
    client = FakeOpenDota()
    build_leaderboard(store, client, 100, now=now, refresh=True)
    assert client.refresh_calls == 0  # кулдаун → в OpenDota не ходили


def test_leaderboard_refreshes_when_stale(store):
    now = 100_000
    p = store.add_player(100, 1, "A", 5000, 1000, 1000)
    store.update_player_rank(p.id, 80, None, updated_ts=now - 100_000)  # давно
    client = FakeOpenDota()
    build_leaderboard(store, client, 100, now=now, refresh=True)
    assert client.refresh_calls >= 1  # устарело → обновили


def test_leaderboard_sorted_by_current_mmr_desc(store):
    now = 100_000
    high = store.add_player(100, 1, "High", anchor_mmr=5000, anchor_ts=1000, created_ts=1000)
    low = store.add_player(100, 2, "Low", anchor_mmr=3000, anchor_ts=1000, created_ts=1000)
    none = store.add_player(100, 3, "None", anchor_mmr=None, anchor_ts=1000, created_ts=1000)
    store.add_matches(high.id, [{"match_id": 10, "start_time": 2000, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 0, "deaths": 0, "assists": 0, "hero_id": 1}])

    board = build_leaderboard(store, FakeOpenDota(), 100, now=now, refresh=False)
    names = [s.display_name for s in board]
    assert names[0] == "High"          # 5025
    assert names[1] == "Low"           # 3000
    assert names[2] == "None"          # None current — в конце


class FakeStratz:
    def __init__(self, data=None, fail=False):
        self.data = data or {}
        self.fail = fail
        self.calls = 0

    def get_matches(self, account_id, match_ids, hints=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("stratz down")
        return {m: v for m, v in self.data.items() if m in match_ids}


def test_refresh_player_enriches_with_stratz(store):
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(matches=[od_match(100, 1000)])
    stratz = FakeStratz({100: {"position": 3, "role": "CORE", "lane": "OFF_LANE", "imp": 7}})
    refresh_player(store, od, player, 2000, stratz=stratz)
    row = store.get_matches(player.id)[0]
    assert (row["position"], row["lane"], row["imp"]) == (3, "OFF_LANE", 7)


def test_refresh_player_survives_stratz_failure(store):
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(matches=[od_match(100, 1000)])
    inserted = refresh_player(store, od, player, 2000, stratz=FakeStratz(fail=True))
    assert inserted == 1
    assert store.get_matches(player.id)[0]["position"] is None


def test_refresh_player_skips_stratz_when_all_matches_enriched(store):
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(matches=[od_match(100, 1000)])
    stratz = FakeStratz({100: {"position": 1, "role": "CORE", "lane": "SAFE_LANE", "imp": 1, "party_size": 1}})
    refresh_player(store, od, player, 2000, stratz=stratz)
    refresh_player(store, od, player, 2100, stratz=stratz)
    assert stratz.calls == 1


# --- представления для /match, /heroes <игрок>, /hero ------------------

def _seed(store, name, acc, rows):
    player = store.add_player(1, acc, name, None, 0, 0)
    store.add_matches(player.id, rows)
    return player


def _row(match_id, start, hero=1, slot=0, rw=True):
    return {"match_id": match_id, "start_time": start, "player_slot": slot,
            "radiant_win": rw, "lobby_type": 7, "hero_id": hero}


def test_build_match_view_latest_across_chat(store):
    from mmrbot.tracker import build_match_view
    _seed(store, "Вася", 1, [_row(10, 100), _row(11, 200)])
    _seed(store, "Петя", 2, [_row(12, 300, hero=5)])
    view = build_match_view(store, 1, None, None)
    assert view["player"].display_name == "Петя" and view["match"]["match_id"] == 12
    assert "party" not in view


def test_build_match_view_by_name_and_id(store):
    from mmrbot.tracker import build_match_view
    _seed(store, "Вася", 1, [_row(10, 100), _row(11, 200)])
    view = build_match_view(store, 1, "вася", 10)
    assert view["match"]["match_id"] == 10
    assert build_match_view(store, 1, "Никто", None) is None
    assert build_match_view(store, 1, "Вася", 999) is None


def test_build_player_heroes_and_hero_view(store):
    from mmrbot.tracker import build_hero_view, build_player_heroes
    _seed(store, "Вася", 1, [_row(10, 100, hero=1), _row(11, 200, hero=1), _row(12, 300, hero=2)])
    _seed(store, "Петя", 2, [_row(13, 100, hero=1, rw=False)])
    player, rows = build_player_heroes(store, 1, "Вася", None)
    assert player.display_name == "Вася" and [r["hero_id"] for r in rows] == [1, 2]
    assert build_player_heroes(store, 1, "нет", None) is None
    _, rows = build_player_heroes(store, 1, "Вася", 250)
    assert [r["hero_id"] for r in rows] == [2]
    entries = build_hero_view(store, 1, 1, None)
    assert [(p.display_name, s["games"]) for p, s in entries] == [("Вася", 2), ("Петя", 1)]


def test_stratz_miss_is_retried_then_given_up(store):
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(matches=[od_match(100, 1000)])
    stratz = FakeStratz({})  # Stratz этот матч не знает
    for i in range(12):  # между попытками проходит достаточно времени, чтобы пауза всегда успевала выйти
        refresh_player(store, od, player, 2000 + i * 100_000, stratz=stratz)
    assert stratz.calls == 8  # дальше max_tries — не спрашиваем
    assert store.get_matches(player.id)[0]["position"] is None


def test_summary_heroes_use_full_history(store):
    player = store.add_player(100, 42, "Вася", 5000, anchor_ts=1000, created_ts=1000)
    client = FakeOpenDota(matches=[od_match(1, 100, hero_id=7), od_match(2, 200, hero_id=7)])
    refresh_player(store, client, player, now=3000)
    summary = build_player_summary(store, store.get_or_create_chat(100), player, now=3000)
    assert [h["hero_id"] for h in summary.top_heroes] == [7]
    assert summary.hero_pool == 1 and summary.games_total == 2
    assert summary.mmr_delta == 0                        # оба матча до якоря — на MMR не влияют


def test_backfill_stratz_fills_pending_across_players_in_batches(store):
    import mmrbot.tracker as tr
    from mmrbot.tracker import backfill_stratz
    store.get_or_create_chat(100)
    player = store.add_player(100, 42, "Вася", None, 0, 0)
    store.add_matches(player.id, [
        {"match_id": i, "start_time": i, "player_slot": 0, "radiant_win": True, "lobby_type": 7}
        for i in range(1, 8)
    ])
    data = {i: {"position": 1, "role": "CORE", "lane": "SAFE_LANE", "imp": i, "party_size": 1} for i in range(1, 8)}
    old = tr.STRATZ_CAP
    tr.STRATZ_CAP = 3
    try:
        stratz = FakeStratz(data)
        backfill_stratz(store, stratz, rounds=2)
        assert sum(1 for m in store.get_matches(player.id) if m["position"]) == 6   # 2 пачки по 3
        backfill_stratz(store, stratz, rounds=2)
        assert all(m["position"] == 1 for m in store.get_matches(player.id))
        calls = stratz.calls
        backfill_stratz(store, stratz, rounds=2)
        assert stratz.calls == calls                       # всё заполнено — лишних запросов нет
    finally:
        tr.STRATZ_CAP = old


def _seed_matches(store, player, n):
    store.add_matches(player.id, [
        {"match_id": i, "start_time": i, "player_slot": 0, "radiant_win": True, "lobby_type": 7}
        for i in range(1, n + 1)
    ])


def test_refresh_enriches_only_few_matches_in_request(store):
    """В пользовательском запросе обогащаем немного — иначе /stats ждёт по ~1с на матч."""
    import mmrbot.tracker as tr
    player = store.add_player(100, 42, "Вася", None, 0, 0)
    _seed_matches(store, player, 30)
    client = FakeOpenDota(match_stats={"gpm": 500, "benchmarks": {"gold_per_min": 0.5}})
    calls = []
    orig = client.get_match_player_stats
    client.get_match_player_stats = lambda m, a, s=None: (calls.append(m), orig(m, a, s))[1]
    refresh_player(store, client, player, now=100)
    assert len(calls) <= tr.ENRICH_CAP <= 4


def test_backfill_opendota_enriches_backlog_across_players(store):
    from mmrbot.tracker import backfill_opendota
    store.get_or_create_chat(100)
    player = store.add_player(100, 42, "Вася", None, 0, 0)
    _seed_matches(store, player, 10)
    client = FakeOpenDota(match_stats={"gpm": 500, "benchmarks": {"gold_per_min": 0.5}})
    assert backfill_opendota(store, client, per_player=6) == 6
    assert sum(1 for m in store.get_matches(player.id) if m["enriched"]) == 6
    backfill_opendota(store, client, per_player=6)
    assert all(m["enriched"] for m in store.get_matches(player.id))
    assert backfill_opendota(store, client, per_player=6) == 0   # всё готово — запросов нет


def test_empty_match_details_do_not_block_queue(store):
    """Матч, по которому OpenDota ничего не отдаёт, сдаётся после нескольких попыток."""
    from mmrbot.tracker import backfill_opendota
    store.get_or_create_chat(100)
    player = store.add_player(100, 42, "Вася", None, 0, 0)
    _seed_matches(store, player, 2)
    client = FakeOpenDota(match_stats=None)               # нет данных ни по одному матчу
    for _ in range(6):
        backfill_opendota(store, client, per_player=5)
    assert store.get_unenriched_match_ids(player.id, 0, 10) == []


# --- скорость: доп. запросы только когда есть что обновлять --------------------

class InsightsOpenDota(FakeOpenDota):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.extra_calls = 0

    def get_totals(self, account_id):
        self.extra_calls += 1
        return {"gpm": 400.0, "xpm": 500.0, "last_hits": 150.0}

    def get_lanes(self, account_id):
        self.extra_calls += 1
        return {1: (3, 2)}

    def get_gpm_distribution(self, account_id):
        self.extra_calls += 1
        return {"median": 400, "best": 700}


def test_refresh_never_requests_unused_career_aggregates(store):
    """Средние GPM/XPM, линии и медиана GPM нигде не показываются — запросов за ними нет."""
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    client = InsightsOpenDota(matches=[od_match(3, 1500)])
    refresh_player(store, client, player, now=3000)
    client.matches.append(od_match(4, 2500))
    refresh_player(store, client, store.get_player(100, "Вася"), now=4000)
    assert client.extra_calls == 0


# --- лидерборд за период (/stats неделя|месяц) --------------------------------

def test_period_leaderboard_counts_only_window_and_sorts_by_delta(store):
    from mmrbot.tracker import build_period_leaderboard

    a = store.add_player(100, 1, "Аня", 5000, 0, 0)
    b = store.add_player(100, 2, "Боря", 5000, 0, 0)
    c = store.add_player(100, 3, "Ваня", 5000, 0, 0)
    now = 10_000_000
    refresh_player(store, FakeOpenDota(matches=[
        od_match(1, now - 100, radiant_win=True),          # в окне: победа
        od_match(2, now - 200, radiant_win=True),          # в окне: победа
        od_match(3, now - 9_000_000, radiant_win=True),    # вне окна
    ]), a, now=now)
    refresh_player(store, FakeOpenDota(matches=[od_match(4, now - 100, radiant_win=False)]), b, now=now)
    refresh_player(store, FakeOpenDota(matches=[]), c, now=now)

    rows = build_period_leaderboard(store, 100, since_ts=now - 7 * 86_400)
    assert [r["name"] for r in rows] == ["Аня", "Боря", "Ваня"]  # плюс → минус → без игр
    assert rows[0]["games"] == 2 and rows[0]["wins"] == 2 and rows[0]["delta"] > 0
    assert rows[1]["losses"] == 1 and rows[1]["delta"] < 0
    assert rows[2]["games"] == 0


def test_summary_today_is_calendar_day_in_chat_tz(store):
    from datetime import datetime, timezone
    now = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())  # 15:00 МСК
    midnight = int(datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc).timestamp())  # 00:00 МСК
    player = store.add_player(100, 42, "Вася", anchor_mmr=5000, anchor_ts=1000, created_ts=1000)
    store.add_matches(player.id, [
        {"match_id": 1, "start_time": midnight - 3600, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 1, "deaths": 1, "assists": 1, "hero_id": 1},  # вчера 23:00 МСК, но <24ч назад
        {"match_id": 2, "start_time": midnight + 3600, "player_slot": 0, "radiant_win": True, "lobby_type": 7, "kills": 1, "deaths": 1, "assists": 1, "hero_id": 2},  # сегодня
    ])
    player = store.get_player(100, "Вася")
    chat = store.get_or_create_chat(100)  # tz по умолчанию Europe/Moscow
    s = build_player_summary(store, chat, player, now=now)
    assert s.games_today == 1


def test_refresh_chat_refreshes_only_stale_players_and_returns_fresh_rows(store):
    from mmrbot.tracker import refresh_chat
    now = 1_000_000
    stale = store.add_player(1, 11, "Старый", None, 0, 0)
    fresh = store.add_player(1, 22, "Свежий", None, 0, 0)
    store.update_player_rank(fresh.id, 80, None, updated_ts=now - 10)
    client = FakeOpenDota()
    players = refresh_chat(store, client, 1, now)
    assert client.profile_calls == 1  # только устаревший
    assert {p.account_id: p.updated_ts for p in players}[11] == now


def test_get_outcomes_returns_light_rows_in_time_order(store):
    p = store.add_player(1, 11, "A", None, 0, 0)
    store.add_matches(p.id, [
        {**od_match(2, 200), "duration": 1}, {**od_match(1, 100, slot=130, radiant_win=False)},
    ])
    rows = store.get_outcomes(p.id)
    assert [r["start_time"] for r in rows] == [100, 200]
    assert set(rows[0]) == {"start_time", "player_slot", "radiant_win"}


# --- достоверность и скорость сбора матчей -------------------------------------

class RecentOpenDota(FakeOpenDota):
    """Клиент с лёгким списком последних матчей (как настоящий OpenDota.get_recent_matches)."""

    def __init__(self, *a, recent=None, **kw):
        super().__init__(*a, **kw)
        self.recent = recent or []
        self.recent_calls = 0
        self.limits = []

    def get_recent_matches(self, account_id):
        self.recent_calls += 1
        return list(self.recent)

    def get_matches(self, account_id, limit=200):
        self.limits.append(limit)
        return super().get_matches(account_id, limit)


def _synced_player(store, client, now=3000):
    """Игрок после первой (полной) загрузки истории."""
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    refresh_player(store, client, player, now=now)
    return store.get_player(100, "Вася")


def test_refresh_uses_light_recent_list_with_match_details(store):
    client = RecentOpenDota(matches=[od_match(1, 1500)])
    player = _synced_player(store, client)
    client.recent = [
        {**od_match(2, 2500), "gold_per_min": 612, "hero_damage": 30500, "hero_healing": 0, "party_size": 2},
        {**od_match(9, 2400, lobby_type=0)},   # не ранкед — мимо
        od_match(1, 1500),                      # перекрытие с сохранённым — пропусков нет
    ]
    assert refresh_player(store, client, player, now=3200) == 1
    assert client.limits == [None]              # тяжёлый список после первой загрузки не трогали
    row = store.get_matches(player.id)[-1]
    assert (row["match_id"], row["gpm"], row["hero_damage"], row["hero_healing"], row["party_size"]) == (
        2, 612, 30500, 0, 2)                    # статистика матча — без запроса на матч


def test_refresh_goes_deep_when_recent_list_does_not_reach_stored_history(store):
    client = RecentOpenDota(matches=[od_match(1, 1500)])
    player = _synced_player(store, client)
    client.matches = [od_match(1, 1500), od_match(2, 2000), od_match(3, 2500)]
    client.recent = [od_match(3, 2500)]         # между 1500 и 2500 могло быть что угодно
    assert refresh_player(store, client, player, now=3200) == 2
    assert client.limits == [None, 200]
    assert {m["match_id"] for m in store.get_matches(player.id)} == {1, 2, 3}


def test_refresh_goes_deep_periodically_for_self_check(store):
    import mmrbot.tracker as tr
    client = RecentOpenDota(matches=[od_match(1, 1500)], recent=[od_match(1, 1500)])
    player = _synced_player(store, client)
    refresh_player(store, client, player, now=3200)
    assert client.limits == [None]
    late = 3000 + tr.DEEP_SYNC_SEC
    refresh_player(store, client, store.get_player(100, "Вася"), now=late)
    assert client.limits == [None, 200]         # раз в DEEP_SYNC_SEC сверяемся по большому списку
    assert store.get_player(100, "Вася").history_ts == late


def test_refresh_loads_full_history_when_gap_exceeds_page(store):
    import mmrbot.tracker as tr
    client = FakeOpenDota(matches=[od_match(1, 100)])
    player = _synced_player(store, client)
    limits = []

    class Gap(FakeOpenDota):
        def get_matches(self, account_id, limit=200):
            limits.append(limit)
            count = 250 if limit is None else limit
            return [od_match(1000 + i, 5000 + i) for i in range(count)]

    assert refresh_player(store, Gap(), player, now=9000) == 250
    assert limits == [tr.HISTORY_LIMIT_REFRESH, None]   # все 200 новые → разрыв → вся история


def test_matches_are_saved_even_if_profile_fails(store):
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)

    class NoProfile(FakeOpenDota):
        def get_profile(self, account_id):
            raise RuntimeError("OpenDota HTTP 503")

    assert refresh_player(store, NoProfile(matches=[od_match(1, 1500)]), player, now=3000) == 1
    assert store.get_player(100, "Вася").updated_ts == 3000


def test_failed_match_fetch_does_not_mark_player_updated(store):
    player = store.add_player(100, 42, "Вася", 5000, 1000, 1000)

    class NoMatches(FakeOpenDota):
        def get_matches(self, account_id, limit=200):
            raise RuntimeError("OpenDota HTTP 503")

    with pytest.raises(RuntimeError):
        refresh_player(store, NoMatches(), player, now=3000)
    assert store.get_player(100, "Вася").updated_ts is None   # кулдаун не выдаст старое за свежее


def test_profile_is_not_refetched_without_new_matches(store):
    import mmrbot.tracker as tr
    client = FakeOpenDota(profile={"rank_tier": 63, "leaderboard_rank": None, "personaname": "Вася"},
                          matches=[od_match(1, 1500)])
    player = _synced_player(store, client)
    assert client.profile_calls == 1
    refresh_player(store, client, player, now=3300)
    assert client.profile_calls == 1                         # игр не было — ранг не менялся
    client.matches.append(od_match(2, 3400))
    refresh_player(store, client, store.get_player(100, "Вася"), now=3600)
    assert client.profile_calls == 2                         # новая игра — ранг мог измениться
    refresh_player(store, client, store.get_player(100, "Вася"), now=3600 + tr.PROFILE_TTL)
    assert client.profile_calls == 3                         # и изредка — для подстраховки


def test_empty_profile_does_not_erase_known_rank(store):
    client = FakeOpenDota(profile={"rank_tier": 63, "leaderboard_rank": None, "personaname": "Вася"})
    player = _synced_player(store, client)
    client.profile = {"rank_tier": None, "leaderboard_rank": None, "personaname": None}  # сбой/скрытый профиль
    client.matches = [od_match(1, 3100)]
    refresh_player(store, client, player, now=3300)
    assert store.get_player(100, "Вася").last_rank_tier == 63


def test_steam_watch_also_updates_rank(store):
    from mmrbot.tracker import detect_steam_changes
    store.get_or_create_chat(100)
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    client = FakeOpenDota(profile={"rank_tier": 71, "leaderboard_rank": None, "personaname": "V"})
    detect_steam_changes(store, client, now=5000)
    got = store.get_player(100, "Вася")
    assert (got.last_rank_tier, got.profile_ts) == (71, 5000)
    assert got.updated_ts is None                            # матчи при этом не сверялись


def test_mmr_counts_game_that_was_in_progress_at_anchor(store):
    # MMR задан в 2000: игра 1 уже закончилась (в MMR учтена), игра 2 ещё шла — её результат прибавляем.
    player = store.add_player(100, 42, "Вася", 5000, anchor_ts=2000, created_ts=2000)
    store.add_matches(player.id, [
        _m(1, 100, duration=1800),                       # кончилась в 1900 — до якоря
        _m(2, 1500, duration=1800),                      # кончилась в 3300 — после якоря
        _m(3, 4000, duration=1800, radiant_win=False),
    ])
    s = build_player_summary(store, store.get_or_create_chat(100), store.get_player(100, "Вася"), now=10_000)
    assert s.mmr_delta == 0 and s.current_mmr == 5000   # +25 за игру 2, −25 за игру 3


def test_fast_refresh_fetches_only_matches_and_background_finishes_the_rest(store):
    from mmrbot.tracker import finish_refresh, refresh_chat
    store.get_or_create_chat(100)
    player = store.add_player(100, 42, "Вася", None, 0, 0)
    _seed_matches(store, player, 5)
    client = InsightsOpenDota(matches=[od_match(99, 500)],
                              match_stats={"gpm": 500, "benchmarks": {"gold_per_min": 0.5}})
    calls = []
    orig = client.get_match_player_stats
    client.get_match_player_stats = lambda m, a, s=None: (calls.append(m), orig(m, a, s))[1]
    stratz = FakeStratz({99: {"position": 2, "role": "CORE", "lane": "MID", "imp": 7, "party_size": 1}})

    refresh_chat(store, client, 100, 1_000_000, stratz, fast=True)
    # Команда пользователя: у OpenDota только матчи (+ профиль, раз есть новая игра) — два запроса.
    assert (client.match_calls, client.profile_calls) == (1, 1)
    assert (client.refresh_calls, client.extra_calls, calls) == (0, 0, [])
    new = next(m for m in store.get_matches(player.id) if m["match_id"] == 99)
    assert new["position"] == 2 and stratz.calls == 1     # новая игра и её позиция (Stratz, пачкой) — сразу

    assert finish_refresh(store, client, 100, per_player=3) == 3   # остальное догоняет фон
    assert (client.refresh_calls, client.extra_calls, len(calls)) == (1, 0, 3)

    client.extra_calls = 0
    finish_refresh(store, client, 100, per_player=0)
    assert client.extra_calls == 0                        # всё актуально — лишних запросов нет


def test_idle_party_is_polled_less_often_without_api_key(store):
    import mmrbot.tracker as tr
    from mmrbot.tracker import detect_new_games
    now = 1_000_000
    store.get_or_create_chat(100)
    p = store.add_player(100, 42, "Вася", 5000, 0, 0)
    store.add_matches(p.id, [_m(1, now - 10 * 3600)])     # последняя игра давно — пати не в сессии
    store.touch_player(p.id, now - 300)
    store.mark_notified(p.id)

    keyless = FakeOpenDota()
    keyless.api_key = None
    detect_new_games(store, keyless, store.get_or_create_chat(100), now)
    assert keyless.match_calls == 0                       # 5 минут < GAME_IDLE_COOLDOWN
    detect_new_games(store, keyless, store.get_or_create_chat(100), now - 300 + tr.GAME_IDLE_COOLDOWN)
    assert keyless.match_calls == 1

    store.touch_player(p.id, now - 300)
    keyed = FakeOpenDota()
    keyed.api_key = "KEY"
    detect_new_games(store, keyed, store.get_or_create_chat(100), now)
    assert keyed.match_calls == 1                         # с ключом лимит не жмёт — опрос частый


def test_active_party_is_polled_often_even_without_api_key(store):
    from mmrbot.tracker import detect_new_games
    now = 1_000_000
    store.get_or_create_chat(100)
    p = store.add_player(100, 42, "Вася", 5000, 0, 0)
    store.add_matches(p.id, [_m(1, now - 3600)])          # играли час назад — сессия идёт
    store.touch_player(p.id, now - 300)
    store.mark_notified(p.id)
    client = FakeOpenDota()
    client.api_key = None
    detect_new_games(store, client, store.get_or_create_chat(100), now)
    assert client.match_calls == 1


# --- аудит данных: точность ---------------------------------------------

def test_best_worst_hour_needs_two_distinct_hours():
    from mmrbot.tracker import _best_worst_hour
    assert _best_worst_hour({10: (5, 3)}) == (None, None)          # один час не бывает и лучшим, и худшим
    assert _best_worst_hour({10: (4, 2), 11: (4, 2)}) == (None, None)  # одинаковый винрейт
    best, worst = _best_worst_hour({10: (4, 3), 11: (4, 1)})
    assert best[0] == 10 and worst[0] == 11


def test_lobby_rank_is_a_real_rank_tier(store):
    """Медиана 45 и 52 — не «Archon 8»: берём реальное значение из набора."""
    import statistics
    from mmrbot.ranks import rank_label
    from mmrbot.tracker import median_rank_tier
    assert median_rank_tier([45, 52]) in (45, 52)
    assert rank_label(median_rank_tier([45, 52])) in ("Archon 5", "Legend 2")
    assert median_rank_tier([]) is None
    assert median_rank_tier([54, 54, 61]) == 54


def test_enrich_failure_does_not_burn_attempts(store):
    """Сбой/лимит OpenDota — не «нет данных»: попытки не тратятся, цикл останавливается."""
    from mmrbot.tracker import backfill_opendota
    store.get_or_create_chat(100)
    player = store.add_player(100, 42, "Вася", None, 0, 0)
    _seed_matches(store, player, 3)
    client = FakeOpenDota()
    calls = []

    def boom(match_id, account_id, player_slot=None):
        calls.append(match_id)
        raise RuntimeError("opendota down")

    client.get_match_player_stats = boom
    for _ in range(6):
        backfill_opendota(store, client, per_player=5)
    assert len(store.get_unenriched_match_ids(player.id, 0, 10)) == 3   # все ещё в очереди
    assert len(calls) == 6                                              # по одному запросу за прогон, не по матчу


def test_stratz_failure_does_not_burn_attempts(store):
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(matches=[od_match(100, 1000)])
    for _ in range(8):
        refresh_player(store, od, player, 2000, stratz=FakeStratz(fail=True))
    assert store.get_match_ids_without_stratz(player.id, 0, 10) == [100]


def test_stratz_miss_backs_off_then_retries(store):
    """Stratz ещё не разобрал матч: повтор не сразу, а через растущую паузу, и позже всё же происходит."""
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(matches=[od_match(100, 1000)])
    stratz = FakeStratz({})                               # матч не найден
    refresh_player(store, od, player, 2000, stratz=stratz)
    assert stratz.calls == 1
    refresh_player(store, od, player, 2100, stratz=stratz)   # пауза не вышла — запроса нет
    assert stratz.calls == 1
    refresh_player(store, od, player, 2000 + 301, stratz=stratz)
    assert stratz.calls == 2
    # паузы растут, а сдаёмся не раньше чем через сутки-двое, а не через 15 минут
    assert sum(store.STRATZ_BACKOFF) >= 86400
    stratz.data = {100: {"position": 1, "role": "CORE", "lane": "SAFE_LANE", "imp": 1, "party_size": 1}}
    refresh_player(store, od, player, 2000 + 301 + 901, stratz=stratz)
    assert store.get_matches(player.id)[0]["position"] == 1


def test_refresh_heroes_uses_opendota_list():
    from mmrbot import heroes
    from mmrbot.tracker import refresh_heroes

    class C:
        def get_heroes(self):
            return [{"id": 9998, "localized_name": "Test Hero"}]

    try:
        assert refresh_heroes(C()) == 1 and heroes.hero_name(9998) == "Test Hero"
    finally:
        heroes.HERO_NAMES.pop(9998, None)


def test_closed_history_flag_saved_from_profile(store):
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    od = FakeOpenDota(profile={"rank_tier": 55, "leaderboard_rank": None, "personaname": "x", "fh_unavailable": True},
                      matches=[od_match(1, 1000)])
    refresh_player(store, od, player, 2000)
    assert store.get_player(1, "Вася").fh_unavailable is True
    chat = store.get_or_create_chat(1)
    s = build_player_summary(store, chat, store.get_player(1, "Вася"), now=2000)
    assert s.history_closed is True
    from mmrbot.formatting import render_player_card
    assert "🔒" in render_player_card(s)
    # профиль без признака (сбой) флаг не сбрасывает
    refresh_player(store, FakeOpenDota(profile={"rank_tier": 55, "leaderboard_rank": None, "personaname": "x"},
                                       matches=[od_match(2, 1500)]), store.get_player(1, "Вася"), 90_000)
    assert store.get_player(1, "Вася").fh_unavailable is True


def test_leaver_status_stored_and_shown(store):
    from mmrbot.formatting import render_match_card
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    store.add_matches(player.id, [{**_m(5, 1000), "leaver_status": 3}])
    row = store.get_matches(player.id)[0]
    assert row["leaver_status"] == 3
    assert "покинул" in render_match_card({"player": player, "match": row})


def test_comparison_ignores_perf_built_on_too_few_games(store):
    """Перф по 3 матчам не ставят рядом с перфом по 500: ниже порога метрика в сравнении не участвует."""
    from dataclasses import replace
    p = store.add_player(100, 1, "Base", 5000, 1000, 1000)
    store.add_matches(p.id, [_m(10, 2000)])
    base = build_leaderboard(store, FakeOpenDota(), 100, now=100_000, refresh=False)[0]
    a = replace(base, display_name="A", avg_perf=0.9, enriched_games=3, avg_gpm_window=900.0, detail_games=2)
    b = replace(base, display_name="B", avg_perf=0.5, enriched_games=50, avg_gpm_window=400.0, detail_games=50)
    comp = build_chat_comparison([a, b])
    assert "perf" not in comp["players"]["A"]["ranks"] and "gpm" not in comp["players"]["A"]["ranks"]
    assert comp["players"]["B"]["ranks"]["perf"] == 1
