import asyncio
from datetime import datetime, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from mmrbot import scheduler as sched
from mmrbot.scheduler import due_local_date
from mmrbot.storage import Chat, Storage


def chat(digest_hour=10, tz="Europe/Moscow", last=None):
    return Chat(chat_id=1, digest_hour=digest_hour, mmr_step=25, tz=tz, last_digest_date=last)


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_due_at_digest_hour():
    # 07:00 UTC == 10:00 МСК
    assert due_local_date(chat(digest_hour=10), utc(2026, 9, 29, 7)) == "2026-09-29"


def test_not_due_before_hour():
    # 06:00 UTC == 09:00 МСК < 10
    assert due_local_date(chat(digest_hour=10), utc(2026, 9, 29, 6)) is None


def test_catch_up_after_hour_same_day():
    # 08:00 UTC == 11:00 МСК, дайджест ещё не слали → догоняем
    assert due_local_date(chat(digest_hour=10), utc(2026, 9, 29, 8)) == "2026-09-29"


def test_not_due_if_already_sent_today():
    c = chat(digest_hour=10, last="2026-09-29")
    assert due_local_date(c, utc(2026, 9, 29, 7)) is None


def test_due_next_day_after_previous_send():
    c = chat(digest_hour=10, last="2026-09-28")
    assert due_local_date(c, utc(2026, 9, 29, 7)) == "2026-09-29"


def test_respects_chat_timezone():
    # Asia/Yekaterinburg = UTC+5; 05:00 UTC == 10:00 YEKT
    c = chat(digest_hour=10, tz="Asia/Yekaterinburg")
    assert due_local_date(c, utc(2026, 9, 29, 5)) == "2026-09-29"


def test_bad_timezone_falls_back_to_moscow():
    c = chat(digest_hour=10, tz="Not/AZone")
    assert due_local_date(c, utc(2026, 9, 29, 7)) == "2026-09-29"


# --- send_digest: устойчивость к потере доступа к чату -----------------------


class _FailingBot:
    def __init__(self, exc):
        self.exc = exc
        self.sent = 0

    async def send_message(self, *a, **kw):
        self.sent += 1
        raise self.exc


def _run_digest(tmp_path, monkeypatch, exc):
    async def fake_board(*a, **kw):
        return "доска"

    monkeypatch.setattr(sched, "render_board", fake_board)
    storage = Storage(str(tmp_path / "t.db"))
    c = storage.get_or_create_chat(5)
    bot = _FailingBot(exc)
    asyncio.run(sched.send_digest(bot, storage, None, c, "2026-09-29"))
    return storage.get_or_create_chat(5).last_digest_date


def test_digest_forbidden_marks_day_done(tmp_path, monkeypatch):
    exc = TelegramForbiddenError(method=SendMessage(chat_id=5, text="x"), message="bot was kicked")
    assert _run_digest(tmp_path, monkeypatch, exc) == "2026-09-29"


def test_digest_chat_not_found_marks_day_done(tmp_path, monkeypatch):
    exc = TelegramBadRequest(method=SendMessage(chat_id=5, text="x"), message="chat not found")
    assert _run_digest(tmp_path, monkeypatch, exc) == "2026-09-29"


def test_digest_other_error_is_retried_next_hour(tmp_path, monkeypatch):
    assert _run_digest(tmp_path, monkeypatch, RuntimeError("boom")) is None
