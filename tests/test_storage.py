import pytest

from mmrbot.storage import Storage


@pytest.fixture
def store(tmp_path):
    return Storage(str(tmp_path / "test.db"))


def match(match_id, start_time, slot=0, radiant_win=True, k=1, d=2, a=3, lobby_type=7, hero_id=1):
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


# --- chats --------------------------------------------------------------

def test_get_or_create_chat_defaults(store):
    chat = store.get_or_create_chat(100)
    assert chat.chat_id == 100
    assert chat.digest_hour == 10
    assert chat.mmr_step == 25
    assert chat.tz == "Europe/Moscow"


def test_set_step_persists(store):
    store.get_or_create_chat(100)
    store.set_chat_step(100, 30)
    assert store.get_or_create_chat(100).mmr_step == 30


def test_set_digest_hour_persists(store):
    store.get_or_create_chat(100)
    store.set_chat_digest_hour(100, 8)
    assert store.get_or_create_chat(100).digest_hour == 8


def test_list_chats(store):
    store.get_or_create_chat(100)
    store.get_or_create_chat(200)
    assert {c.chat_id for c in store.list_chats()} == {100, 200}


def test_last_digest_date_default_none(store):
    assert store.get_or_create_chat(100).last_digest_date is None


def test_set_last_digest_date_persists(store):
    store.get_or_create_chat(100)
    store.set_last_digest_date(100, "2026-09-29")
    assert store.get_or_create_chat(100).last_digest_date == "2026-09-29"


# --- players ------------------------------------------------------------

def test_add_and_get_player(store):
    p = store.add_player(chat_id=100, account_id=42, display_name="Вася", anchor_mmr=5000, anchor_ts=1000, created_ts=1000)
    assert p.id > 0
    assert p.account_id == 42
    assert p.anchor_mmr == 5000
    got = store.get_player(100, "Вася")
    assert got.id == p.id


def test_get_player_case_insensitive(store):
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    assert store.get_player(100, "вАсЯ") is not None


def test_get_player_by_account_id(store):
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    assert store.get_player(100, "42").account_id == 42


def test_get_player_unicode_digit_no_crash(store):
    # str.isdigit() шире int(): '²' (U+00B2) не должен ронять get_player.
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    assert store.get_player(100, "²") is None


def test_get_player_by_account_id_exact_ignores_numeric_name(store):
    # Игрок с именем-числом не должен перехватывать поиск по account_id другого игрока.
    store.add_player(100, 555, "Alice", 5000, 1000, 1000)
    store.add_player(100, 999, "555", 4000, 1000, 1000)
    found = store.get_player_by_account_id(100, 555)
    assert found.display_name == "Alice"
    assert found.account_id == 555


def test_duplicate_account_raises(store):
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    with pytest.raises(ValueError):
        store.add_player(100, 42, "Вася2", 4000, 1000, 1000)


def test_same_account_different_chats_ok(store):
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_player(200, 42, "Вася", 5000, 1000, 1000)  # другой чат — ок
    assert store.get_player(200, "42") is not None


def test_list_players_scoped_to_chat(store):
    store.add_player(100, 1, "A", None, 1000, 1000)
    store.add_player(100, 2, "B", None, 1000, 1000)
    store.add_player(200, 3, "C", None, 1000, 1000)
    assert {p.account_id for p in store.list_players(100)} == {1, 2}


def test_remove_player(store):
    store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    assert store.remove_player(100, "Вася") is True
    assert store.get_player(100, "Вася") is None
    assert store.remove_player(100, "Вася") is False  # уже нет


def test_set_player_anchor(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.set_player_anchor(p.id, anchor_mmr=5300, anchor_ts=2000)
    got = store.get_player(100, "Вася")
    assert got.anchor_mmr == 5300
    assert got.anchor_ts == 2000


def test_update_player_rank(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.update_player_rank(p.id, rank_tier=75, leaderboard_rank=None, updated_ts=3000)
    got = store.get_player(100, "Вася")
    assert got.last_rank_tier == 75
    assert got.updated_ts == 3000


# --- matches ------------------------------------------------------------

def test_add_matches_and_read_all(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    inserted = store.add_matches(p.id, [match(1, 1100), match(2, 1200)])
    assert inserted == 2
    rows = store.get_matches(p.id)
    assert len(rows) == 2


def test_add_matches_is_idempotent(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1100)])
    inserted = store.add_matches(p.id, [match(1, 1100), match(2, 1200)])
    assert inserted == 1  # только новый матч
    assert len(store.get_matches(p.id)) == 2


def test_get_matches_since_ts_filters(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1000), match(2, 2000), match(3, 3000)])
    recent = store.get_matches(p.id, since_ts=2000)
    assert {r["match_id"] for r in recent} == {2, 3}


def test_get_matches_returns_fields_for_aggregate(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1100, slot=132, radiant_win=True, k=7, d=3, a=9)])
    row = store.get_matches(p.id)[0]
    assert row["player_slot"] == 132
    assert bool(row["radiant_win"]) is True
    assert (row["kills"], row["deaths"], row["assists"]) == (7, 3, 9)


def test_update_match_details_and_read(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1100)])
    store.update_match_details(p.id, 1, {
        "gpm": 500, "xpm": 600, "last_hits": 180, "denies": 10,
        "hero_damage": 25000, "tower_damage": 3000, "hero_healing": 0,
        "net_worth": 18000, "level": 25,
    }, perf_score=0.72)
    row = store.get_matches(p.id)[0]
    assert row["gpm"] == 500
    assert row["net_worth"] == 18000
    assert row["perf_score"] == 0.72
    assert row["enriched"] == 1


def test_update_match_details_stores_benchmarks_json(store):
    import json
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1100)])
    store.update_match_details(p.id, 1, {"gpm": 500, "benchmarks": {"gold_per_min": 0.6}}, perf_score=0.6)
    row = store.get_matches(p.id)[0]
    assert json.loads(row["bench_json"]) == {"gold_per_min": 0.6}


def test_get_unenriched_match_ids(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1000), match(2, 2000), match(3, 3000)])
    store.update_match_details(p.id, 2, {"gpm": 400}, perf_score=0.5)
    ids = store.get_unenriched_match_ids(p.id, since_ts=0, limit=10)
    assert set(ids) == {1, 3}  # матч 2 уже обогащён


def test_get_unenriched_respects_window_and_limit(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.add_matches(p.id, [match(1, 1000), match(2, 2000), match(3, 3000)])
    assert store.get_unenriched_match_ids(p.id, since_ts=2000, limit=10) == [3, 2] or \
        set(store.get_unenriched_match_ids(p.id, since_ts=2000, limit=10)) == {2, 3}
    assert len(store.get_unenriched_match_ids(p.id, since_ts=0, limit=1)) == 1


def test_add_matches_stores_duration_and_party_size(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    m = match(1, 1100)
    m["duration"] = 2400
    m["party_size"] = 3
    m["average_rank"] = 74
    store.add_matches(p.id, [m])
    row = store.get_matches(p.id)[0]
    assert row["duration"] == 2400
    assert row["party_size"] == 3
    assert row["average_rank"] == 74


def test_update_player_totals(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.update_player_totals(p.id, gpm=520.5, xpm=610.0, last_hits=180.2)
    got = store.get_player(100, "Вася")
    assert got.last_gpm == pytest.approx(520.5)
    assert got.last_xpm == pytest.approx(610.0)
    assert got.last_last_hits == pytest.approx(180.2)


def test_update_player_insights(store):
    p = store.add_player(100, 42, "Вася", 5000, 1000, 1000)
    store.update_player_insights(p.id, lanes_json='{"2": [10, 6]}', gpm_median=520, gpm_best=800)
    got = store.get_player(100, "Вася")
    assert got.last_lanes == '{"2": [10, 6]}'
    assert got.last_gpm_median == 520
    assert got.last_gpm_best == 800


def test_update_match_stratz_sets_fields_and_keeps_existing_numbers(tmp_path):
    from mmrbot.storage import Storage
    store = Storage(str(tmp_path / "s.db"))
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    store.add_matches(player.id, [
        {"match_id": 10, "start_time": 5, "player_slot": 0, "radiant_win": True, "lobby_type": 7},
    ])
    store.update_match_details(player.id, 10, {"gpm": 500}, None)
    store.update_match_stratz(player.id, 10, {
        "position": 2, "role": "CORE", "lane": "MID_LANE", "imp": 9,
        "gpm": 999, "xpm": 700, "net_worth": 20000,
    })
    row = store.get_matches(player.id)[0]
    assert (row["position"], row["role"], row["lane"], row["imp"]) == (2, "CORE", "MID_LANE", 9)
    assert row["gpm"] == 500  # уже заполненное из OpenDota не затираем
    assert row["xpm"] == 700 and row["net_worth"] == 20000  # пустое дозаполняем


def test_matches_without_stratz_listed(tmp_path):
    from mmrbot.storage import Storage
    store = Storage(str(tmp_path / "s.db"))
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    store.add_matches(player.id, [
        {"match_id": 10, "start_time": 5, "player_slot": 0, "radiant_win": True, "lobby_type": 7},
        {"match_id": 11, "start_time": 6, "player_slot": 0, "radiant_win": True, "lobby_type": 7},
    ])
    store.update_match_stratz(player.id, 10, {"position": 1, "role": "CORE", "lane": "SAFE_LANE", "imp": 1, "party_size": 1})
    assert store.get_match_ids_without_stratz(player.id, 0) == [11]


def test_stratz_miss_counter_limits_pending(tmp_path):
    from mmrbot.storage import Storage
    store = Storage(str(tmp_path / "s.db"))
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    store.add_matches(player.id, [
        {"match_id": 10, "start_time": 5, "player_slot": 0, "radiant_win": True, "lobby_type": 7},
    ])
    for _ in range(3):
        store.mark_stratz_miss(player.id, [10])
    assert store.get_match_ids_without_stratz(player.id, 0, max_tries=5) == [10]
    assert store.get_match_ids_without_stratz(player.id, 0, max_tries=3) == []


def test_stratz_fills_missing_party_size_but_keeps_opendota_value(tmp_path):
    from mmrbot.storage import Storage
    store = Storage(str(tmp_path / "s.db"))
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    base = {"player_slot": 0, "radiant_win": True, "lobby_type": 7}
    store.add_matches(player.id, [
        dict(base, match_id=10, start_time=5),                  # размера пати нет
        dict(base, match_id=11, start_time=6, party_size=2),    # из OpenDota
    ])
    for mid in (10, 11):
        store.update_match_stratz(player.id, mid, {"position": 1, "party_size": 3})
    sizes = {m["match_id"]: m["party_size"] for m in store.get_matches(player.id)}
    assert sizes == {10: 3, 11: 2}


def test_stratz_done_matches_without_party_size_are_requeued(tmp_path):
    from mmrbot.storage import Storage
    store = Storage(str(tmp_path / "s.db"))
    player = store.add_player(1, 42, "Вася", None, 0, 0)
    store.add_matches(player.id, [{"match_id": 10, "start_time": 5, "player_slot": 0,
                                   "radiant_win": True, "lobby_type": 7}])
    store.update_match_stratz(player.id, 10, {"position": 1})          # старый разбор: позиция есть, пати нет
    assert store.get_match_ids_without_stratz(player.id, 0) == [10]
    store.update_match_stratz(player.id, 10, {"position": 1, "party_size": 1})
    assert store.get_match_ids_without_stratz(player.id, 0) == []


def test_connection_uses_wal_and_busy_timeout(tmp_path):
    from mmrbot.storage import Storage

    store = Storage(str(tmp_path / "w.db"))
    conn = store._conn()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000
    finally:
        conn.close()
