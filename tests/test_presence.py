"""Оповещение о заходе в Dota 2 (Steam Web API) и улучшенное оповещение о конце матча."""
import pytest

from mmrbot.formatting import render_game_alert, render_settings, render_start_alert
from mmrbot.keyboards import settings_menu
from mmrbot.presence import MISS_LIMIT, advance
from mmrbot.steam import Steam, parse_in_dota
from mmrbot.storage import Storage
from mmrbot.tracker import detect_presence

NOW = 1_000_000
BASE = 76561197960265728


@pytest.fixture
def store(tmp_path):
    st = Storage(str(tmp_path / "p.db"))
    st.get_or_create_chat(100)
    return st


# --- чистая логика переходов --------------------------------------------

def test_advance_start_stay_and_end_with_debounce():
    assert advance(None, 0, True, NOW) == (NOW, 0, "start")
    assert advance(NOW, 0, True, NOW + 120) == (NOW, 0, None)
    since, misses, event = advance(NOW, 0, False, NOW + 120)
    assert (since, misses, event) == (NOW, 1, None)  # один пропуск — ещё не конец (дребезг)
    assert advance(NOW, MISS_LIMIT - 1, False, NOW + 240) == (None, 0, "end")
    assert advance(NOW, 1, True, NOW + 240) == (NOW, 0, None)  # вернулся — пропуски сброшены
    assert advance(None, 0, False, NOW) == (None, 0, None)


def test_advance_hidden_profile_keeps_state():
    assert advance(NOW, 1, None, NOW + 60) == (NOW, 1, None)
    assert advance(None, 0, None, NOW + 60) == (None, 0, None)


# --- Steam Web API ------------------------------------------------------

def test_parse_in_dota():
    data = {"response": {"players": [
        {"steamid": str(BASE + 1), "communityvisibilitystate": 3, "gameid": "570"},
        {"steamid": str(BASE + 2), "communityvisibilitystate": 3},
        {"steamid": str(BASE + 3), "communityvisibilitystate": 3, "gameid": "730"},
        {"steamid": str(BASE + 4), "communityvisibilitystate": 1},
    ]}}
    got = parse_in_dota(data, [1, 2, 3, 4, 5])
    assert got == {1: True, 2: False, 3: False, 4: None, 5: None}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return _Resp(self.payload)


def test_steam_client_batches_ids_and_never_leaks_key_in_result():
    session = _Session({"response": {"players": [{"steamid": str(BASE + 1), "communityvisibilitystate": 3, "gameid": "570"}]}})
    steam = Steam("SECRET", session=session)
    assert steam.get_in_dota([1]) == {1: True}
    url, params = session.calls[0]
    assert "GetPlayerSummaries" in url and params["steamids"] == str(BASE + 1) and params["key"] == "SECRET"
    assert steam.get_in_dota([]) == {} and len(session.calls) == 1


# --- детектор -----------------------------------------------------------

class FakeSteam:
    def __init__(self, states):
        self.states = states

    def get_in_dota(self, ids):
        return {i: self.states.get(i) for i in ids}


def _two(store):
    store.add_player(100, 1, "Вася", 5000, 0, 0)
    store.add_player(100, 2, "Петя", 5000, 0, 0)
    store.add_player(100, 3, "Коля", 5000, 0, 0)


def test_detect_presence_groups_starts_and_reports_party_count(store):
    _two(store)
    events = detect_presence(store, FakeSteam({1: True, 2: True, 3: False}), NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev["chat_id"] == 100 and set(ev["names"]) == {"Вася", "Петя"}
    assert ev["in_game"] == 2 and ev["total"] == 3
    assert detect_presence(store, FakeSteam({1: True, 2: True, 3: False}), NOW + 120) == []  # уже оповещали


def test_detect_presence_new_joiner_is_announced_alone(store):
    _two(store)
    detect_presence(store, FakeSteam({1: True}), NOW)
    events = detect_presence(store, FakeSteam({1: True, 3: True}), NOW + 120)
    assert [e["names"] for e in events] == [["Коля"]] and events[0]["in_game"] == 2


def test_detect_presence_after_session_end_announces_again(store):
    store.add_player(100, 1, "Вася", 5000, 0, 0)
    detect_presence(store, FakeSteam({1: True}), NOW)
    for i in range(MISS_LIMIT):
        assert detect_presence(store, FakeSteam({1: False}), NOW + 120 * (i + 1)) == []
    assert len(detect_presence(store, FakeSteam({1: True}), NOW + 1000)) == 1


def test_detect_presence_respects_toggle_and_hidden(store):
    _two(store)
    store.set_chat_notify_start(100, False)
    assert detect_presence(store, FakeSteam({1: True}), NOW) == []
    store.set_chat_notify_start(100, True)
    assert detect_presence(store, FakeSteam({}), NOW) == []  # все скрыты — тишина


# --- рендер -------------------------------------------------------------

def test_render_start_alert():
    one = render_start_alert({"names": ["Вася"], "in_game": 1, "total": 5})
    assert "🟢" in one and "Вася" in one and "зашёл" in one and "1/5" not in one
    two = render_start_alert({"names": ["Вася", "Петя"], "in_game": 3, "total": 5})
    assert "<b>Вася</b>, <b>Петя</b>" in two and "зашли" in two and "3/5" in two


def test_render_game_alert_header_result_and_stats():
    event = {"match_id": 777, "duration": 2280, "shared": None, "rows": [
        {"name": "Вася", "hero_id": 1, "kills": 12, "deaths": 3, "assists": 8, "won": True, "step": 25,
         "current_mmr": 5075, "streak_type": "W", "streak_len": 1, "gpm": 612.4, "hero_damage": 31250, "position": 1},
        {"name": "Петя", "hero_id": 2, "kills": 3, "deaths": 9, "assists": 14, "won": True, "step": 25,
         "current_mmr": None, "streak_type": "W", "streak_len": 1},
    ]}
    text = render_game_alert(event)
    assert "Матч завершён" in text and "Победа" in text
    assert "38 мин" in text and "dotabuff.com/matches/777" in text and "opendota.com/matches/777" in text
    assert "612 GPM" in text and "31.2k" in text and "Pos 1" in text
    assert "GPM" not in text.split("Петя")[1]  # нет данных — не выдумываем
    event["rows"][1]["won"] = False
    assert "Разные стороны" in render_game_alert(event)
    event["rows"][0]["won"] = False
    assert "Поражение" in render_game_alert(event)


def test_game_alert_extras_imp_leaver_mvp_and_lobby_rank():
    event = {"match_id": 5, "duration": 1800, "shared": None, "average_rank": 55, "rows": [
        {"name": "Вася", "hero_id": 1, "kills": 5, "deaths": 5, "assists": 5, "won": True, "step": 25,
         "current_mmr": None, "streak_type": "W", "streak_len": 1, "imp": 12},
        {"name": "Петя", "hero_id": 2, "kills": 10, "deaths": 1, "assists": 12, "won": True, "step": 25,
         "current_mmr": None, "streak_type": "W", "streak_len": 1, "imp": 31},
        {"name": "Коля", "hero_id": 3, "kills": 0, "deaths": 9, "assists": 1, "won": True, "step": 25,
         "current_mmr": None, "streak_type": "W", "streak_len": 1, "leaver_status": 3},
    ]}
    text = render_game_alert(event)
    assert "Лобби:" in text and "Legend 5" in text
    assert "IMP +31" in text and "IMP +12" in text
    assert "⚠️" in text.split("Коля")[1] and "покинул" in text
    mvp_line = [ln for ln in text.splitlines() if "Петя" in ln][0]
    assert "⭐" in mvp_line
    assert text.count("⭐") == 1


def test_game_alert_no_mvp_for_solo_row():
    event = {"match_id": 5, "duration": 1800, "shared": None, "rows": [
        {"name": "Вася", "hero_id": 1, "kills": 5, "deaths": 5, "assists": 5, "won": True, "step": 25,
         "current_mmr": None, "streak_type": "W", "streak_len": 1}]}
    text = render_game_alert(event)
    assert "⭐" not in text and "Лобби" not in text


def test_start_alert_hidden_from_settings_but_stored(store):
    chat = store.get_or_create_chat(100)
    assert chat.notify_start is True  # включается наличием STEAM_API_KEY; в UI не выведено
    assert "Dota 2: " not in render_settings(chat)
    datas = [b.callback_data for row in settings_menu(chat).inline_keyboard for b in row]
    assert "s:start" not in datas
