import asyncio

from mmrbot.bot import DeleteCommandMiddleware


class Msg:
    def __init__(self, text):
        self.text = text
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _run(msg, fail=False):
    async def handler(event, data):
        assert not msg.deleted  # удаляем только после ответа
        if fail:
            raise RuntimeError("boom")
        return "ok"

    return asyncio.run(DeleteCommandMiddleware()(handler, msg, {}))


def test_command_message_deleted_after_handler():
    msg = Msg("/menu")
    assert _run(msg) == "ok"
    assert msg.deleted


def test_command_with_args_and_bot_suffix_deleted():
    msg = Msg("/stats@keigroupsbot неделя")
    _run(msg)
    assert msg.deleted


def test_plain_text_not_deleted():
    msg = Msg("5400")
    _run(msg)
    assert not msg.deleted


def test_delete_failure_is_swallowed():
    class NoRights(Msg):
        async def delete(self):
            raise RuntimeError("no rights")

    assert _run(NoRights("/menu")) == "ok"
