"""Теги участников чата с MMR: формат, привязка Telegram-аккаунта к игроку, синхронизация."""
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest

from mmrbot import commands as cmd
from mmrbot.storage import Storage
from mmrbot.tags import format_tag, sync_member_tags

NOW = 1_000_000


@pytest.fixture
def store(tmp_path):
    st = Storage(str(tmp_path / "t.db"))
    st.get_or_create_chat(100)
    return st


class FakeBot:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def set_chat_member_tag(self, chat_id, user_id, tag=None):
        if user_id in self.fail_for:
            raise TelegramBadRequest(method=None, message="Bad Request: not enough rights")
        self.calls.append((chat_id, user_id, tag))
        return True


# --- формат -------------------------------------------------------------

def test_format_tag_is_short_and_has_no_emoji():
    assert format_tag(5420) == "5420 MMR"
    assert format_tag(0) == "0 MMR"
    assert len(format_tag(20000)) <= 16
    assert format_tag(None) is None


def test_parse_tags_arg():
    assert cmd.parse_on_off("on") is True
    assert cmd.parse_on_off("вкл") is True
    assert cmd.parse_on_off("off") is False
    assert cmd.parse_on_off("выкл") is False
    with pytest.raises(ValueError):
        cmd.parse_on_off("")
    with pytest.raises(ValueError):
        cmd.parse_on_off("maybe")


# --- хранилище ----------------------------------------------------------

def test_link_user_moves_between_players(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    b = store.add_player(100, 2, "Петя", 4000, NOW, NOW)
    store.link_user(100, a.id, 555)
    assert store.list_players(100)[0].tg_user_id == 555
    store.link_user(100, b.id, 555)  # один аккаунт — один игрок в чате
    players = store.list_players(100)
    assert players[0].tg_user_id is None and players[1].tg_user_id == 555


def test_link_user_steals_from_other_user(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    store.link_user(100, a.id, 555)
    store.link_user(100, a.id, 777)
    assert store.list_players(100)[0].tg_user_id == 777


def test_unlink_user_returns_player_and_clears(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    store.link_user(100, a.id, 555)
    store.set_player_tag(a.id, "5000 MMR")
    player = store.unlink_user(100, 555)
    assert player.id == a.id
    again = store.list_players(100)[0]
    assert again.tg_user_id is None and again.last_tag is None
    assert store.unlink_user(100, 555) is None


def test_chat_tag_flag_default_off(store):
    assert store.get_or_create_chat(100).tag_mmr is False
    store.set_chat_tag_mmr(100, True)
    assert store.get_or_create_chat(100).tag_mmr is True
    assert [c.chat_id for c in store.list_chats() if c.tag_mmr] == [100]


# --- синхронизация ------------------------------------------------------

def test_sync_sets_tags_only_for_linked_and_skips_unchanged(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    store.add_player(100, 2, "Петя", 4000, NOW, NOW)  # не привязан
    store.link_user(100, a.id, 555)
    bot = FakeBot()

    assert asyncio.run(sync_member_tags(bot, store, 100, NOW)) == 1
    assert bot.calls == [(100, 555, "5000 MMR")]

    assert asyncio.run(sync_member_tags(bot, store, 100, NOW)) == 0  # тег не изменился — запроса нет
    assert len(bot.calls) == 1

    store.set_player_anchor(a.id, 5100, NOW)
    assert asyncio.run(sync_member_tags(bot, store, 100, NOW)) == 1
    assert bot.calls[-1] == (100, 555, "5100 MMR")


def test_sync_skips_player_without_mmr(store):
    a = store.add_player(100, 1, "Вася", None, NOW, NOW)
    store.link_user(100, a.id, 555)
    bot = FakeBot()
    assert asyncio.run(sync_member_tags(bot, store, 100, NOW)) == 0
    assert bot.calls == []


def test_sync_survives_rejected_member(store):
    a = store.add_player(100, 1, "Админ", 6000, NOW, NOW)
    b = store.add_player(100, 2, "Петя", 4000, NOW, NOW)
    store.link_user(100, a.id, 111)
    store.link_user(100, b.id, 222)
    bot = FakeBot(fail_for={111})  # админа/владельца тегом не поменять

    assert asyncio.run(sync_member_tags(bot, store, 100, NOW)) == 1
    assert bot.calls == [(100, 222, "4000 MMR")]


# --- кнопки -------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import mmrbot.bot as botmod  # noqa: E402
from mmrbot.formatting import render_settings  # noqa: E402
from mmrbot.keyboards import CATEGORIES, settings_menu  # noqa: E402


class _Msg:
    class chat:
        id = 100
        type = "supergroup"

    def __init__(self):
        self.sent, self.edited = [], []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))
        return self

    async def edit_text(self, text, **kw):
        self.edited.append((text, kw))
        return self


class _CB:
    def __init__(self, data, bot, user_id=555):
        self.data, self.message, self.bot = data, _Msg(), bot
        self.from_user = SimpleNamespace(id=user_id)

    async def answer(self, *a, **k):
        pass


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _texts(msg):
    return [t for t, _ in msg.sent + msg.edited]


def test_settings_menu_has_tags_toggle(store):
    chat = store.get_or_create_chat(100)
    assert "Теги" in render_settings(chat)
    btn = next(b for b in _buttons(settings_menu(chat)) if b.callback_data == "s:tags")
    assert "выкл" in btn.text
    store.set_chat_tag_mmr(100, True)
    btn = next(b for b in _buttons(settings_menu(store.get_or_create_chat(100))) if b.callback_data == "s:tags")
    assert "вкл" in btn.text


def test_party_menu_has_me_button():
    actions = [action for row in CATEGORIES["party"][1] for _, action in row]
    assert "me" in actions


def test_button_toggle_tags_syncs_on_enable(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    store.link_user(100, a.id, 555)
    bot = FakeBot()
    cb = _CB("s:tags", bot)
    asyncio.run(botmod.on_callback(cb, store, object()))
    assert store.get_or_create_chat(100).tag_mmr is True
    assert bot.calls == [(100, 555, "5000 MMR")]
    asyncio.run(botmod.on_callback(_CB("s:tags", bot), store, object()))
    assert store.get_or_create_chat(100).tag_mmr is False


def test_button_me_picker_and_link_and_unlink(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    bot = FakeBot()

    picker = _CB("m:me", bot)
    asyncio.run(botmod.on_callback(picker, store, object()))
    data = [b.callback_data for b in _buttons((picker.message.edited or picker.message.sent)[-1][1]["reply_markup"])]
    assert "pp:me:1" in data and "pp:meoff:0" in data

    store.set_chat_tag_mmr(100, True)
    asyncio.run(botmod.on_callback(_CB("pp:me:1", bot), store, object()))
    assert store.list_players(100)[0].tg_user_id == 555
    assert bot.calls == [(100, 555, "5000 MMR")]

    asyncio.run(botmod.on_callback(_CB("pp:meoff:0", bot), store, object()))
    assert store.list_players(100)[0].tg_user_id is None
    assert bot.calls[-1] == (100, 555, None)  # тег снят


# --- автопривязка по нику -----------------------------------------------

from mmrbot.tags import auto_link_user  # noqa: E402


def _user(uid, username=None, first="Иван", last=None):
    full = f"{first} {last}" if last else first
    return SimpleNamespace(id=uid, username=username, first_name=first, last_name=last, full_name=full, is_bot=False)


def test_auto_link_by_username_and_name_case_insensitive(store):
    store.add_player(100, 1, "vasya_pro", 5000, NOW, NOW)
    store.add_player(100, 2, "Петя", 4000, NOW, NOW)
    assert auto_link_user(store, 100, _user(10, username="Vasya_Pro")).display_name == "vasya_pro"
    assert auto_link_user(store, 100, _user(20, first="петя")).display_name == "Петя"
    assert [p.tg_user_id for p in store.list_players(100)] == [10, 20]


def test_auto_link_skips_linked_user_taken_player_and_no_match(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    store.add_player(100, 2, "Петя", 4000, NOW, NOW)
    assert auto_link_user(store, 100, _user(10, first="Никто")) is None
    assert auto_link_user(store, 100, _user(10, first="Вася")) is not None
    assert auto_link_user(store, 100, _user(11, first="Вася")) is None  # ник уже занят другим аккаунтом
    assert auto_link_user(store, 100, _user(10, first="Петя")) is None  # у аккаунта уже есть привязка
    assert store.list_players(100)[0].tg_user_id == 10 and a.id == store.list_players(100)[0].id


def test_middleware_links_on_message_only_when_tags_on(store):
    store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    mw = botmod.AutoLinkMiddleware()
    msg = _Msg()
    msg.from_user = _user(10, first="Вася")

    async def handler(event, data):
        return "ok"

    assert asyncio.run(mw(handler, msg, {"storage": store})) == "ok"
    assert store.list_players(100)[0].tg_user_id is None  # теги выключены — не трогаем

    store.set_chat_tag_mmr(100, True)
    assert asyncio.run(mw(handler, msg, {"storage": store})) == "ok"
    assert store.list_players(100)[0].tg_user_id == 10


# --- привязка автора /add -----------------------------------------------

from mmrbot.tags import link_adder  # noqa: E402


def test_link_adder_only_when_user_has_no_player(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    assert link_adder(store, 100, a, _user(10)) is True
    b = store.add_player(100, 2, "Петя", 4000, NOW, NOW)
    assert link_adder(store, 100, b, _user(10)) is False  # друга добавил тот, кто уже привязан к себе
    assert [p.tg_user_id for p in store.list_players(100)] == [10, None]


def test_link_adder_ignores_bots_and_missing_user(store):
    a = store.add_player(100, 1, "Вася", 5000, NOW, NOW)
    assert link_adder(store, 100, a, None) is False
    assert link_adder(store, 100, a, SimpleNamespace(id=1, is_bot=True)) is False
    assert store.list_players(100)[0].tg_user_id is None


def test_do_add_links_author(store, monkeypatch):
    monkeypatch.setattr(botmod, "resolve_account_id", lambda ident: 777)
    monkeypatch.setattr(botmod, "refresh_player", lambda *a, **k: None)
    msg = _Msg()
    msg.from_user = _user(10)
    asyncio.run(botmod.do_add(msg, store, object(), "777 Вася 5000"))
    assert store.list_players(100)[0].tg_user_id == 10
