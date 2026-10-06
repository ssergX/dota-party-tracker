import asyncio

import pytest
from aiogram.filters import CommandObject

import mmrbot.bot as botmod
from mmrbot.formatting import country_flag, render_steam_profile
from mmrbot.opendota import OpenDota
from mmrbot.storage import Storage
from tests.test_opendota import FakeSession

PROFILE = {
    "personaname": "Shinoame",
    "avatarfull": "https://avatars.steamstatic.com/abc_full.jpg",
    "profileurl": "https://steamcommunity.com/id/shinoame/",
    "steamid": "76561199065808320",
    "loccountrycode": "RU",
    "plus": True,
    "last_login": "2026-09-30T18:20:00.000Z",
    "rank_tier": 75,
    "leaderboard_rank": None,
}


def test_get_profile_returns_steam_fields():
    raw = {"profile": {"personaname": "Вася", "avatarfull": "http://a/f.jpg", "profileurl": "http://p/",
                       "steamid": "7656", "loccountrycode": "RU", "plus": True, "last_login": None},
           "rank_tier": 75}
    od = OpenDota(session=FakeSession(raw), min_interval=0)
    profile = od.get_profile(42)
    assert profile["avatarfull"] == "http://a/f.jpg"
    assert profile["profileurl"] == "http://p/"
    assert profile["steamid"] == "7656"
    assert profile["loccountrycode"] == "RU" and profile["plus"] is True


def test_country_flag():
    assert country_flag("RU") == "\U0001F1F7\U0001F1FA"
    assert country_flag(None) == "" and country_flag("xx1") == ""


def test_render_steam_profile_shows_current_nick_and_links():
    text = render_steam_profile("Вася", 105, PROFILE, "Divine 5")
    assert "Shinoame" in text and "Вася" in text  # ник в Steam и ник в боте
    assert "steamcommunity.com/id/shinoame" in text
    assert "76561199065808320" in text and "Divine 5" in text
    assert "Dota Plus" in text


def test_render_steam_profile_handles_missing_fields():
    text = render_steam_profile("Вася", 105, {"personaname": None}, "Без ранга")
    assert "Вася" in text and "105" in text


class FakeOD:
    def get_profile(self, account_id):
        return PROFILE


class Msg:
    class chat:
        id = 100

    def __init__(self):
        self.photos, self.texts = [], []

    async def answer(self, text, **kw):
        self.texts.append(text)
        return self

    async def answer_photo(self, photo, **kw):
        self.photos.append((photo, kw))
        return self

    async def delete(self):
        pass


@pytest.fixture
def storage(tmp_path):
    st = Storage(str(tmp_path / "s.db"))
    st.get_or_create_chat(100)
    st.add_player(100, 105, "Вася", 5000, 0, 0)
    return st


def test_cmd_steam_sends_avatar_photo_with_caption(storage):
    msg = Msg()
    asyncio.run(botmod.cmd_steam(msg, CommandObject(command="steam", args="Вася"), storage, FakeOD()))
    assert len(msg.photos) == 1
    photo, kw = msg.photos[0]
    assert photo == PROFILE["avatarfull"]
    assert "Shinoame" in kw["caption"] and kw["parse_mode"] == "HTML"


def test_cmd_steam_without_avatar_falls_back_to_text(storage):
    class NoAvatar(FakeOD):
        def get_profile(self, account_id):
            return {"personaname": "Shinoame"}

    msg = Msg()
    asyncio.run(botmod.cmd_steam(msg, CommandObject(command="steam", args="Вася"), storage, NoAvatar()))
    assert not msg.photos and any("Shinoame" in t for t in msg.texts)


def test_cmd_steam_unknown_player(storage):
    msg = Msg()
    asyncio.run(botmod.cmd_steam(msg, CommandObject(command="steam", args="Никто"), storage, FakeOD()))
    assert any("Не нашёл" in t for t in msg.texts)
