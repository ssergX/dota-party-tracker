import asyncio

import pytest
import pytz

import mmrbot.bot as botmod
from mmrbot.formatting import render_player_card, render_settings, render_steam_change
from mmrbot.keyboards import TIMEZONES, main_menu, settings_menu
from mmrbot.storage import Storage
from mmrbot.tracker import detect_steam_changes


@pytest.fixture
def store(tmp_path):
    st = Storage(str(tmp_path / "x.db"))
    st.get_or_create_chat(100)
    return st


# --- storage ------------------------------------------------------------

def test_chat_defaults_and_setters(store):
    chat = store.get_or_create_chat(100)
    assert chat.notify_steam is True
    store.set_chat_tz(100, "Asia/Almaty")
    store.set_chat_notify_steam(100, False)
    chat = store.get_or_create_chat(100)
    assert chat.tz == "Asia/Almaty" and chat.notify_steam is False


def test_update_player_steam_first_fill_is_silent_then_detects_changes(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    assert store.update_player_steam(p.id, "Old", "http://a/1.jpg") == {}
    assert store.update_player_steam(p.id, "Old", "http://a/1.jpg") == {}  # без изменений
    changes = store.update_player_steam(p.id, "New", "http://a/2.jpg")
    assert changes == {"name": ("Old", "New"), "avatar": True}
    got = store.get_player(100, "Вася")
    assert got.steam_name == "New" and got.steam_avatar == "http://a/2.jpg"


def test_update_player_steam_ignores_empty_values(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    store.update_player_steam(p.id, "Old", "http://a/1.jpg")
    assert store.update_player_steam(p.id, None, None) == {}
    assert store.get_player(100, "Вася").steam_name == "Old"


# --- детектор смены ника/аватарки ---------------------------------------

class FakeClient:
    def __init__(self, profiles):
        self.profiles = profiles
        self.calls = []

    def get_profile(self, account_id):
        self.calls.append(account_id)
        return self.profiles[account_id]


def test_detect_steam_changes_reports_only_enabled_chats_and_fetches_once(store):
    store.get_or_create_chat(200)
    store.set_chat_notify_steam(200, False)
    a = store.add_player(100, 1, "Вася", None, 0, 0)
    b = store.add_player(200, 1, "Vasya", None, 0, 0)  # тот же аккаунт в другом чате
    store.update_player_steam(a.id, "Old", "http://a/1.jpg")
    store.update_player_steam(b.id, "Old", "http://a/1.jpg")
    client = FakeClient({1: {"personaname": "New", "avatarfull": "http://a/1.jpg"}})
    events = detect_steam_changes(store, client)
    assert client.calls == [1]  # профиль аккаунта запрошен один раз
    assert len(events) == 1 and events[0]["chat_id"] == 100
    assert events[0]["changes"] == {"name": ("Old", "New")}
    # у выключенного чата базовое значение всё равно обновлено — без «пачки» оповещений при включении
    assert store.get_player(200, "Vasya").steam_name == "New"


def test_detect_steam_changes_survives_profile_errors(store):
    a = store.add_player(100, 1, "Вася", None, 0, 0)
    b = store.add_player(100, 2, "Петя", None, 0, 0)

    class Flaky(FakeClient):
        def get_profile(self, account_id):
            if account_id == 1:
                raise RuntimeError("OpenDota down")
            return super().get_profile(account_id)

    events = detect_steam_changes(store, Flaky({2: {"personaname": "P", "avatarfull": "x"}}))
    assert events == []
    assert store.get_player(100, "Петя").steam_name == "P"


# --- рендер -------------------------------------------------------------

def test_render_steam_change_name_and_avatar(store):
    p = store.add_player(100, 1, "Вася", None, 0, 0)
    text = render_steam_change(p, {"name": ("Old", "New"), "avatar": True})
    assert "Вася" in text and "Old" in text and "New" in text and "аватар" in text


def test_player_card_shows_steam_nick():
    from tests.test_formatting import summary
    text = render_player_card(summary(steam_name="Shinoame"))
    assert "Steam" in text and "Shinoame" in text


def test_render_settings_shows_values(store):
    store.set_chat_step(100, 30)
    store.set_chat_digest_hour(100, 9)
    text = render_settings(store.get_or_create_chat(100))
    assert "⚙️" in text and "±30" in text and "09:00" in text and "Europe/Moscow" in text
    assert "включены" in text


# --- клавиатуры ---------------------------------------------------------

def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_main_menu_has_settings_button():
    assert "m:settings" in {b.callback_data for b in _flat(main_menu())}


def test_settings_menu_buttons_short_and_marked(store):
    chat = store.get_or_create_chat(100)
    buttons = _flat(settings_menu(chat))
    data = {b.callback_data for b in buttons}
    assert {"s:step:25", "s:hour:-1", "s:hour:1", "s:steam", "m:menu", "x:close"} <= data
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert next(b for b in buttons if b.callback_data == "s:step:25").text.startswith("•")


def test_timezones_are_valid_pytz_names():
    for _, name in TIMEZONES:
        pytz.timezone(name)


# --- обработчики --------------------------------------------------------

class Msg:
    class chat:
        id = 100

    def __init__(self):
        self.sent, self.edited = [], []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))
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


def press(store, data):
    cb = CB(data)
    asyncio.run(botmod.on_callback(cb, store, object()))
    return cb


def test_menu_opens_settings(store):
    cb = press(store, "m:settings")
    assert "⚙️" in cb.message.sent[0][0] and cb.message.sent[0][1]["reply_markup"] is not None


@pytest.mark.parametrize("data,check", [
    ("s:step:50", lambda c: c.mmr_step == 50),
    ("s:hour:1", lambda c: c.digest_hour == 11),
    ("s:hour:-1", lambda c: c.digest_hour == 9),
    ("s:tz:0", lambda c: c.tz == TIMEZONES[0][1]),
    ("s:steam", lambda c: c.notify_steam is False),
])
def test_settings_buttons_change_chat_and_edit_in_place(store, data, check):
    cb = press(store, data)
    assert check(store.get_or_create_chat(100))
    assert cb.message.edited and not cb.message.sent  # правим то же сообщение


def test_settings_hour_wraps_around_midnight(store):
    store.set_chat_digest_hour(100, 23)
    press(store, "s:hour:1")
    assert store.get_or_create_chat(100).digest_hour == 0


def test_settings_rejects_bad_values(store):
    press(store, "s:step:7")
    press(store, "s:tz:99")
    press(store, "s:hour:5")
    chat = store.get_or_create_chat(100)
    assert (chat.mmr_step, chat.tz, chat.digest_hour) == (25, "Europe/Moscow", 10)
