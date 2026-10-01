"""Новые игры, достижения, недельная сводка, график, бэкап, ссылки и склонения."""
import asyncio
import sqlite3
from datetime import date, datetime, timezone

import pytest
from aiogram.filters import CommandObject

import mmrbot.bot as botmod
from mmrbot import achievements
from mmrbot.backup import backup_db
from mmrbot.charts import render_mmr_chart
from mmrbot.formatting import (
    plural_heroes,
    render_achievement_alert,
    render_achievements,
    render_game_alert,
    render_match_card,
    render_player_card,
    render_player_heroes,
    render_weekly,
)
from mmrbot.keyboards import graph_buttons, settings_menu
from mmrbot.scheduler import due_weekly_key
from mmrbot.stats import mmr_series
from mmrbot.storage import Storage
from mmrbot.tracker import (
    build_mmr_series,
    build_weekly_report,
    check_achievements,
    detect_new_games,
    list_achievements,
)
from tests.test_formatting import summary
from tests.test_tracker import FakeOpenDota, od_match

NOW = 1_000_000


def m(match_id, start, win=True, hero=1, k=5, d=3, a=7, dur=2400):
    return {"match_id": match_id, "start_time": start, "player_slot": 0, "radiant_win": win, "lobby_type": 7,
            "kills": k, "deaths": d, "assists": a, "hero_id": hero, "duration": dur}


@pytest.fixture
def store(tmp_path):
    st = Storage(str(tmp_path / "f.db"))
    st.get_or_create_chat(100)
    return st


# --- достижения (правила) -----------------------------------------------

def test_win_streak_and_lose_streak_achievements():
    wins = [m(i, i * 100, True) for i in range(1, 7)]
    got = achievements.evaluate(wins)
    assert "win_streak_5" in got and "win_streak_10" not in got and "lose_streak_5" not in got
    losses = [m(i, i * 100, False) for i in range(1, 6)]
    assert "lose_streak_5" in achievements.evaluate(losses)


def test_game_count_and_hero_achievements():
    matches = [m(i, i * 100, i % 2 == 0, hero=1) for i in range(1, 101)]
    got = achievements.evaluate(matches)
    assert {"games_50", "games_100", "hero_50", "hero_100"} <= set(got)
    assert "games_250" not in got and "Anti-Mage" in got["hero_100"]


def test_single_game_achievements():
    got = achievements.evaluate([
        m(1, 100, k=22, d=2, a=5), m(2, 200, k=1, d=21, a=3), m(3, 300, k=8, d=0, a=4, dur=3700),
    ])
    assert {"kills_20", "deaths_20", "deathless", "marathon"} <= set(got)


# --- достижения (хранение) ----------------------------------------------

def test_check_achievements_first_run_is_silent_then_reports_new(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    store.add_matches(p.id, [m(i, i * 100) for i in range(1, 5)])
    assert check_achievements(store, p, NOW) == []  # засев без оповещений
    store.add_matches(p.id, [m(5, 500)])
    new = check_achievements(store, p, NOW)
    assert [code for code, _ in new] == ["win_streak_5"]
    assert check_achievements(store, p, NOW) == []  # второй раз — уже известно


def test_list_achievements_does_not_write(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    store.add_matches(p.id, [m(i, i * 100) for i in range(1, 7)])
    rows = list_achievements(store, 100, NOW)
    assert rows[0][0] == "Вася" and "win_streak_5" in rows[0][1]
    assert store.get_achievements(p.id) == {}
    assert list_achievements(store, 100, NOW, name="Нет такого") == []


def test_remove_player_deletes_achievements(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    store.add_achievements(p.id, {"games_50": "50"}, NOW)
    store.remove_player(100, "Вася")
    assert store.get_achievements(p.id) == {}


# --- миграция «оповещённых» матчей --------------------------------------

def test_migration_marks_existing_matches_as_notified(tmp_path):
    path = str(tmp_path / "old.db")
    st = Storage(path)
    p = st.add_player(100, 1, "Вася", None, 0, 0)
    st.add_matches(p.id, [m(1, 100)])
    with sqlite3.connect(path) as conn:  # имитируем БД старой схемы: без колонки notified
        conn.execute("ALTER TABLE matches DROP COLUMN notified")
    st2 = Storage(path)  # миграция
    assert st2.get_unnotified_matches(p.id, 0) == []


# --- новые игры ---------------------------------------------------------

def _chat_with_two(store):
    a = store.add_player(100, 1, "Вася", 5000, 0, 0)
    b = store.add_player(100, 2, "Петя", 5000, 0, 0)
    return a, b


def test_detect_new_games_groups_party_match_and_marks_notified(store):
    _chat_with_two(store)
    client = FakeOpenDota(matches=[od_match(777, NOW - 600, slot=0, radiant_win=True, k=12, d=3, a=8)])
    chat = store.get_or_create_chat(100)
    events = detect_new_games(store, client, chat, NOW)
    assert len(events) == 1 and events[0]["kind"] == "match"
    ev = events[0]
    assert {r["name"] for r in ev["rows"]} == {"Вася", "Петя"}
    assert ev["shared"]["games"] == 1 and ev["shared"]["wins"] == 1
    assert detect_new_games(store, client, store.get_or_create_chat(100), NOW + 400) == []  # уже оповещали


def test_detect_new_games_ignores_old_matches(store):
    store.add_player(100, 1, "Вася", 5000, 0, 0)
    client = FakeOpenDota(matches=[od_match(1, NOW - 10 * 3600)])
    assert detect_new_games(store, client, store.get_or_create_chat(100), NOW) == []
    p = store.get_player(100, "Вася")
    assert store.get_unnotified_matches(p.id, 0) == []  # помечен, чтобы не всплыть позже


def test_detect_new_games_respects_toggle(store):
    store.add_player(100, 1, "Вася", 5000, 0, 0)
    store.set_chat_notify_games(100, False)
    client = FakeOpenDota(matches=[od_match(1, NOW - 60)])
    assert detect_new_games(store, client, store.get_or_create_chat(100), NOW) == []
    assert client.match_calls == 0  # при выключенных оповещениях OpenDota не дёргаем


def test_detect_new_games_survives_refresh_error(store):
    store.add_player(100, 1, "Вася", 5000, 0, 0)

    class Down(FakeOpenDota):
        def get_profile(self, account_id):
            raise RuntimeError("down")

    assert detect_new_games(store, Down(), store.get_or_create_chat(100), NOW) == []


# --- рендер оповещений --------------------------------------------------

def test_render_game_alert():
    event = {"match_id": 777, "duration": 2280, "shared": {"games": 5, "wins": 3, "losses": 2}, "rows": [
        {"name": "Вася", "hero_id": 1, "kills": 12, "deaths": 3, "assists": 8, "won": True, "step": 25,
         "current_mmr": 5075, "streak_type": "W", "streak_len": 3},
        {"name": "Петя", "hero_id": 2, "kills": 3, "deaths": 9, "assists": 14, "won": False, "step": 25,
         "current_mmr": None, "streak_type": "L", "streak_len": 1},
    ]}
    text = render_game_alert(event)
    assert "38 мин" in text and "dotabuff.com/matches/777" in text
    assert "🏆" in text and "💀" in text and "12/3/8" in text
    assert "+25" in text and "-25" in text and "≈5075 MMR" in text and "3 подряд" in text
    assert "Сегодня вместе: 5 игр · 3–2" in text


def test_render_achievement_alert_normal_and_anti():
    good = render_achievement_alert({"name": "Вася", "items": [("win_streak_5", "5")]})
    bad = render_achievement_alert({"name": "Вася", "items": [("deaths_20", "Axe 1/21/3")]})
    assert "получил достижение" in good and "5 побед подряд" in good
    assert "антирекорд" in bad and "Axe 1/21/3" in bad


def test_render_achievements_list():
    rows = [("Вася", {"win_streak_5": (NOW, "5")}), ("Петя", {})]
    text = render_achievements(rows)
    assert "Вася" in text and "5 побед подряд" in text and "пока нет" in text


# --- недельная сводка ---------------------------------------------------

def test_weekly_report_and_render(store):
    a, b = _chat_with_two(store)
    day = 86_400
    store.add_matches(a.id, [m(1, NOW - day, True, hero=1), m(2, NOW - 2 * day, True, hero=1),
                             m(3, NOW - 3 * day, False, hero=2), m(9, NOW - 20 * day, True)])
    store.add_matches(b.id, [m(1, NOW - day, True, hero=1), m(4, NOW - 4 * day, False, hero=3)])
    report = build_weekly_report(store, 100, NOW)
    assert report["hero"]["hero_id"] == 1 and report["hero"]["games"] == 3
    assert report["streak"] == ("Вася", 2)
    assert report["shared"]["games"] == 1
    text = render_weekly(report)
    assert "Итоги недели" in text and "Больше всех поднялся" in text and "Герой недели" in text
    assert "Anti-Mage" in text and "Лучшая серия" in text and "Вместе" in text


def test_weekly_report_empty_week(store):
    store.add_player(100, 1, "Вася", None, 0, 0)
    assert "не было" in render_weekly(build_weekly_report(store, 100, NOW))


def test_due_weekly_key(store):
    chat = store.get_or_create_chat(100)  # 10:00, Москва
    monday_noon = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)  # пн 12:00 МСК
    key = due_weekly_key(chat, monday_noon)
    assert key == "2026-W40"
    assert due_weekly_key(chat, datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)) is None  # пн 08:00 — рано
    assert due_weekly_key(chat, datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)) is None  # вторник
    store.set_last_weekly(100, key)
    assert due_weekly_key(store.get_or_create_chat(100), monday_noon) is None  # уже отправляли


# --- график -------------------------------------------------------------

def test_mmr_series_cumulative():
    matches = [m(1, 100, True), m(2, 200, False), m(3, 300, True), m(4, 400, True)]
    assert mmr_series(matches, 25) == [(100, 25), (200, 0), (300, 25), (400, 50)]


def test_build_mmr_series_skips_players_without_games(store):
    a, _ = _chat_with_two(store)
    store.add_matches(a.id, [m(1, 100), m(2, 200, False)])
    assert build_mmr_series(store, 100, None) == {"Вася": [(100, 25), (200, 0)]}
    assert build_mmr_series(store, 100, 150) == {"Вася": [(200, -25)]}


def test_render_mmr_chart_returns_png():
    png = render_mmr_chart({"Вася": [(NOW, 25), (NOW + 3600, 0)], "Петя": [(NOW, -25)]}, "Динамика", "Europe/Moscow")
    assert png.startswith(b"\x89PNG") and len(png) > 2000


def test_graph_buttons_mark_current():
    buttons = [b for row in graph_buttons("month").inline_keyboard for b in row]
    assert next(b for b in buttons if b.callback_data == "g:month").text.startswith("•")


# --- склонения и ссылки -------------------------------------------------

@pytest.mark.parametrize("n,word", [(1, "герой"), (2, "героя"), (5, "героев"), (11, "героев"),
                                    (21, "герой"), (102, "героя"), (112, "героев")])
def test_plural_heroes(n, word):
    assert plural_heroes(n) == word


def test_heroes_tail_uses_plural():
    rows = [{"hero_id": i, "games": 5, "winrate": 0.5, "kda": 3.0} for i in range(1, 13)]
    assert "и ещё 2 героя" in render_player_heroes("Вася", "all", rows, limit=10)


def test_dotabuff_links_in_player_card_and_match_card():
    assert "dotabuff.com/players/42" in render_player_card(summary())
    row = {"match_id": 555, "start_time": NOW, "player_slot": 0, "radiant_win": True, "kills": 1, "deaths": 1,
           "assists": 1, "hero_id": 1}
    from types import SimpleNamespace
    text = render_match_card({"player": SimpleNamespace(display_name="Вася"), "match": row})
    assert "dotabuff.com/matches/555" in text


# --- бэкап --------------------------------------------------------------

def test_backup_creates_prunes_and_is_idempotent(tmp_path):
    db = str(tmp_path / "bot.db")
    Storage(db).get_or_create_chat(1)
    for day in (1, 2, 3, 4):
        assert backup_db(db, keep=2, today=date(2026, 10, day)) is not None
    names = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert names == ["bot-20261003.db", "bot-20261004.db"]
    assert backup_db(db, keep=2, today=date(2026, 10, 4)) is None  # за сегодня уже есть
    restored = Storage(str(tmp_path / "backups" / "bot-20261004.db"))
    assert restored.get_or_create_chat(1).chat_id == 1


def test_backup_disabled_or_missing_db(tmp_path):
    assert backup_db(str(tmp_path / "none.db"), keep=7) is None
    db = str(tmp_path / "bot.db")
    Storage(db)
    assert backup_db(db, keep=0) is None


# --- настройки и обработчики -------------------------------------------

def test_settings_menu_has_new_toggles(store):
    data = {b.callback_data for row in settings_menu(store.get_or_create_chat(100)).inline_keyboard for b in row}
    assert {"s:games", "s:weekly"} <= data


class Msg:
    class chat:
        id = 100

    def __init__(self):
        self.sent, self.edited, self.photos = [], [], []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))
        return self

    async def answer_photo(self, photo, **kw):
        self.photos.append((photo, kw))
        return self

    async def edit_text(self, text, **kw):
        self.edited.append((text, kw))

    async def delete(self):
        pass


class CB:
    def __init__(self, data):
        self.data, self.message = data, Msg()

    async def answer(self, *a, **k):
        pass


def test_settings_toggle_games_and_weekly(store):
    for data, field in (("s:games", "notify_games"), ("s:weekly", "notify_weekly")):
        cb = CB(data)
        asyncio.run(botmod.on_callback(cb, store, object()))
        assert getattr(store.get_or_create_chat(100), field) is False
        assert cb.message.edited


def test_cmd_achievements_lists_players(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    store.add_matches(p.id, [m(i, i * 100) for i in range(1, 7)])
    msg = Msg()
    asyncio.run(botmod.cmd_achievements(msg, CommandObject(command="achievements", args=None), store))
    assert "5 побед подряд" in msg.sent[0][0]
    msg = Msg()
    asyncio.run(botmod.cmd_achievements(msg, CommandObject(command="achievements", args="Никто"), store))
    assert "не найден" in msg.sent[0][0]


class FakeOD:
    def refresh(self, account_id):
        return True

    def get_profile(self, account_id):
        return {"rank_tier": None, "leaderboard_rank": None, "personaname": None}

    def get_matches(self, account_id, limit=200):
        return []


def test_cmd_graph_sends_photo_for_period(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    now = int(datetime.now(timezone.utc).timestamp())
    store.add_matches(p.id, [m(1, now - 3600), m(2, now - 1800, False)])
    msg = Msg()
    asyncio.run(botmod.cmd_graph(msg, CommandObject(command="graph", args="неделя"), store, FakeOD()))
    assert len(msg.photos) == 1 and "Динамика MMR за неделю" in msg.photos[0][1]["caption"]


def test_cmd_graph_without_games(store):
    store.add_player(100, 1, "Вася", None, 0, 0)
    msg = Msg()
    asyncio.run(botmod.cmd_graph(msg, CommandObject(command="graph", args=None), store, FakeOD()))
    assert not msg.photos and any("не было" in t for t, _ in msg.sent)


def test_graph_period_button_replaces_message(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    now = int(datetime.now(timezone.utc).timestamp())
    store.add_matches(p.id, [m(1, now - 3600)])
    cb = CB("g:month")
    asyncio.run(botmod.on_callback(cb, store, FakeOD()))
    assert len(cb.message.photos) == 1


class PhotoMsg(Msg):
    photo = [object()]  # сообщение с картинкой — график правится на месте

    def __init__(self):
        super().__init__()
        self.media, self.captions, self.deleted = [], [], False

    async def edit_media(self, media, **kw):
        self.media.append((media, kw))

    async def edit_caption(self, **kw):
        self.captions.append(kw)

    async def delete(self):
        self.deleted = True


def test_graph_period_switch_edits_photo_in_place(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    now = int(datetime.now(timezone.utc).timestamp())
    store.add_matches(p.id, [m(1, now - 3600), m(2, now - 1800, False)])
    cb = CB("g:month")
    cb.message = PhotoMsg()
    asyncio.run(botmod.on_callback(cb, store, FakeOD()))
    assert not cb.message.deleted and not cb.message.photos  # ничего не удаляем и не шлём заново
    media, kw = cb.message.media[0]
    assert "за месяц" in media.caption
    marked = [b for row in kw["reply_markup"].inline_keyboard for b in row if b.text.startswith("•")]
    assert marked[0].callback_data == "g:month"


def test_graph_period_switch_without_games_updates_caption(store):
    store.add_player(100, 1, "Вася", None, 0, 0)
    cb = CB("g:day")
    cb.message = PhotoMsg()
    asyncio.run(botmod.on_callback(cb, store, FakeOD()))
    assert "не было" in cb.message.captions[0]["caption"] and not cb.message.deleted


def test_graph_period_uses_games_from_before_anchor(store):
    p = store.add_player(100, 1, "Вася", 5000, anchor_ts=1000, created_ts=1000)
    store.add_matches(p.id, [m(1, 500), m(2, 600, False), m(3, 1500)])
    assert [ts for ts, _ in build_mmr_series(store, 100, 400)["Вася"]] == [500, 600, 1500]  # период шире якоря
    assert [ts for ts, _ in build_mmr_series(store, 100, None)["Вася"]] == [500, 600, 1500]  # «всё» — вся история


# --- рекорды за период ---------------------------------------------------

def rec(match_id, start, **kw):
    """Матч с полями рекордов (для чистых функций, без БД)."""
    base = m(match_id, start)
    base.update(kw)
    return base


def put(store, player, match_id, start, gpm=None, **kw):
    """Матч в БД; GPM записывается как обогащённая деталь."""
    store.add_matches(player.id, [dict(m(match_id, start), **kw)])
    if gpm is not None:
        store.update_match_details(player.id, match_id, {"gpm": gpm}, None)


def test_compute_records_picks_best_game_with_hero_and_match():
    from mmrbot.records import compute_records
    data = compute_records([
        ("Вася", [rec(1, 100, gpm=700, hero_id=1, kills=10), rec(2, 200, gpm=812, hero_id=5, kills=25)]),
        ("Петя", [rec(3, 300, gpm=650, hero_id=2, kills=12, deaths=21)]),
    ])
    by_key = {r["key"]: r for r in data["records"]}
    assert by_key["gpm"]["player"] == "Вася" and by_key["gpm"]["match"]["match_id"] == 2
    assert by_key["gpm"]["text"] == "812 GPM"
    assert by_key["kills"]["match"]["match_id"] == 2
    assert by_key["deaths"]["player"] == "Петя" and by_key["deaths"]["anti"]
    assert data["streak"] == ("Вася", 2)


def test_compute_records_kda_needs_minimum_involvement_and_skips_missing():
    from mmrbot.records import compute_records
    data = compute_records([("Вася", [rec(1, 100, kills=1, deaths=0, assists=2)])])
    assert "kda" not in {r["key"] for r in data["records"]}
    assert "gpm" not in {r["key"] for r in data["records"]}  # у матча нет GPM
    assert compute_records([("Вася", [])]) == {"records": [], "streak": None}


def test_build_records_respects_period(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    put(store, p, 1, NOW - 40 * 86_400, gpm=900)
    put(store, p, 2, NOW - 86_400, gpm=600)
    from mmrbot.tracker import build_records
    week = {r["key"]: r for r in build_records(store, 100, NOW - 7 * 86_400)["records"]}
    assert week["gpm"]["match"]["match_id"] == 2
    alltime = {r["key"]: r for r in build_records(store, 100, None)["records"]}
    assert alltime["gpm"]["match"]["match_id"] == 1


def test_render_records_has_hero_match_link_and_period():
    from mmrbot.formatting import render_records
    from mmrbot.records import compute_records
    text = render_records(compute_records([("Вася", [rec(777, NOW, gpm=812, hero_id=5)])]), "week")
    assert "за неделю" in text and "812 GPM" in text and "dotabuff.com/matches/777" in text
    assert "нет данных" in render_records({"records": [], "streak": None}, "year")


def test_year_period_words_and_since():
    from mmrbot.commands import parse_target_period
    from mmrbot.stats import period_since
    assert parse_target_period("год") == (None, "year")
    assert period_since("year", 1_000_000_000) == 1_000_000_000 - 365 * 86_400


def test_cmd_records_and_period_buttons_edit_in_place(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    now = int(datetime.now(timezone.utc).timestamp())
    put(store, p, 5, now - 3600, gpm=700)
    msg = Msg()
    asyncio.run(botmod.cmd_records(msg, CommandObject(command="records", args="месяц"), store, FakeOD()))
    assert any("Рекорды пати за месяц" in t for t, _ in msg.sent)
    cb = CB("r:year")
    asyncio.run(botmod.on_callback(cb, store, FakeOD()))
    assert "за год" in cb.message.edited[0][0] and not cb.message.sent
    marked = [b for row in cb.message.edited[0][1]["reply_markup"].inline_keyboard for b in row if b.text.startswith("•")]
    assert marked[0].callback_data == "r:year"


# --- «Пульс пати» вместо «Отличий» -------------------------------------

def test_party_pulse_for_single_player():
    from mmrbot.formatting import render_party_pulse
    from mmrbot.records import compute_records
    s = summary(recent_form=[True, True, False, True, True], games_today=3, wins_today=2, losses_today=1,
                delta_today=25)
    rows = [{"name": "Вася", "games": 10, "wins": 6, "losses": 4, "delta": 50, "winrate": 0.6, "kda": 3.0}]
    records = compute_records([("Вася", [rec(7, NOW, gpm=800, hero_id=5, kills=20)])])
    text = render_party_pulse([s], rows, records)
    assert "Пульс пати" in text and "Сегодня: 3 игры · 2–1" in text and "За неделю: 10 игр · 6–4" in text
    assert "🟢🟢🔴🟢🟢" in text  # хронология слева направо, новые справа
    assert "800 GPM" in text and "dotabuff.com/matches/7" in text and "/records" in text
    assert "Лидер недели" not in text  # один игрок — лидера нет


def test_party_pulse_leader_and_quiet_day():
    from mmrbot.formatting import render_party_pulse
    rows = [{"name": "Вася", "games": 4, "wins": 3, "losses": 1, "delta": 50, "winrate": 0.75, "kda": 3.0},
            {"name": "Петя", "games": 4, "wins": 1, "losses": 3, "delta": -50, "winrate": 0.25, "kda": 2.0}]
    quiet = summary(games_today=0, wins_today=0, losses_today=0, delta_today=0, recent_form=[])
    text = render_party_pulse([quiet], rows, {"records": [], "streak": None})
    assert "игр пока не было" in text and "Лидер недели: <b>Вася</b>" in text


def test_chart_thinning_keeps_extremes_and_ends():
    from mmrbot.charts import _thin
    pts = [(i, (i % 50) * (1 if i % 2 else -1)) for i in range(2000)]
    thin = _thin(pts)
    assert len(thin) <= 340 and thin[0] == pts[0] and thin[-1] == pts[-1]
    assert max(v for _, v in thin) >= max(v for _, v in pts) - 5
    assert _thin(pts[:50]) == pts[:50]  # короткие серии не трогаем


def test_chart_long_history_renders_png():
    pts = [(1_000_000 + i * 600, (i % 40) - 20) for i in range(3000)]
    assert render_mmr_chart({"Вася": pts}, "Динамика", "Europe/Moscow").startswith(b"\x89PNG")


def test_graph_cache_avoids_second_render(store, monkeypatch):
    import mmrbot.service as service
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    now = int(datetime.now(timezone.utc).timestamp())
    store.add_matches(p.id, [m(1, now - 3600)])
    calls = []
    monkeypatch.setattr(service, "render_mmr_chart", lambda *a, **k: calls.append(1) or b"\x89PNG")
    service._graph_cache.clear()
    for _ in range(2):
        asyncio.run(service.render_graph_board(store, FakeOD(), 100, "week", refresh=False))
    assert len(calls) == 1
