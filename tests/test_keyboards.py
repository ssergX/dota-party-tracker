from mmrbot.keyboards import main_menu, parse_callback, period_buttons, players_picker
from mmrbot.storage import Player


def _p(i, name):
    return Player(i, 1, 1000 + i, name, None, 0, 0, None, None, None)


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_main_menu_has_actions_and_short_callbacks():
    buttons = _flat(main_menu())
    data = {b.callback_data for b in buttons}
    assert {"m:stats", "m:today", "m:compare", "m:together", "m:heroes", "m:match",
            "m:player", "m:list", "m:help", "m:week", "m:month"} <= data
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_players_picker_uses_account_ids_and_menu_back():
    markup = players_picker([_p(1, "Вася"), _p(2, "Петя"), _p(3, "Коля")], "heroes")
    buttons = _flat(markup)
    assert [b.text for b in buttons[:3]] == ["Вася", "Петя", "Коля"]
    assert buttons[0].callback_data == "pp:heroes:1001"
    assert {b.callback_data for b in buttons[-2:]} == {"m:menu", "x:close"}


def test_period_buttons_mark_current_and_encode_target():
    markup = period_buttons("hp", 1001, "week")
    buttons = _flat(markup)
    assert [b.callback_data for b in buttons[:4]] == [
        "hp:1001:day", "hp:1001:week", "hp:1001:month", "hp:1001:all"]
    assert buttons[1].text.startswith("•")
    assert not buttons[0].text.startswith("•")


def test_parse_callback():
    assert parse_callback("m:stats") == ("m", ["stats"])
    assert parse_callback("hp:1001:week") == ("hp", ["1001", "week"])
    assert parse_callback("") == ("", [])
