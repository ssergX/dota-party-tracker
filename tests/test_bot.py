import asyncio

import mmrbot.bot as botmod
from aiogram.filters import CommandObject


class FakeChat:
    id = 100


class FakeMessage:
    def __init__(self):
        self.chat = FakeChat()
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeStorage:
    def list_players(self, chat_id):
        return [object()]  # непусто, чтобы дойти до сбора статистики


def test_cmd_stats_handles_render_error_gracefully(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("OpenDota down")

    monkeypatch.setattr(botmod, "render_board", boom)
    msg = FakeMessage()
    asyncio.run(botmod.cmd_stats(msg, CommandObject(command="stats"), FakeStorage(), object()))
    # хендлер не должен падать; пользователь получает понятное сообщение об ошибке
    assert any(("не удалось" in t.lower()) or ("ошибка" in t.lower()) for t in msg.sent)


def test_cmd_today_handles_render_error_gracefully(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("OpenDota down")

    monkeypatch.setattr(botmod, "render_board", boom)
    msg = FakeMessage()
    asyncio.run(botmod.cmd_stats(msg, CommandObject(command="today"), FakeStorage(), object()))
    assert any(("не удалось" in t.lower()) or ("ошибка" in t.lower()) for t in msg.sent)
