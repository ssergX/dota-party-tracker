import asyncio
import threading
import time

from mmrbot import service


def _spy(monkeypatch):
    state = {"running": 0, "max": 0}
    guard = threading.Lock()

    def fake_build(storage, od, chat_id, now, refresh, stratz, fast=False):
        with guard:
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
        time.sleep(0.05)
        with guard:
            state["running"] -= 1
        return []

    monkeypatch.setattr(service, "build_leaderboard", fake_build)
    return state


def test_same_chat_refreshes_are_serialized(monkeypatch):
    state = _spy(monkeypatch)

    async def go():
        await asyncio.gather(*(service.gather_summaries(None, None, 1) for _ in range(3)))

    asyncio.run(go())
    assert state["max"] == 1


def test_different_chats_run_in_parallel(monkeypatch):
    state = _spy(monkeypatch)

    async def go():
        await asyncio.gather(service.gather_summaries(None, None, 1), service.gather_summaries(None, None, 2))

    asyncio.run(go())
    assert state["max"] == 2


def test_match_board_escapes_names_in_empty_message(tmp_path):
    from mmrbot.storage import Storage

    store = Storage(str(tmp_path / "e.db"))
    store.add_player(100, 1, "<b>Вася", 5000, 0, 0)  # матчей нет
    text = asyncio.run(service.render_match_board(store, _NoRefresh(), 100, None, None))
    assert "<b>Вася" not in text and "&lt;b&gt;Вася" in text


class _NoRefresh:
    pass
