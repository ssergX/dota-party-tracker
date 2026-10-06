from mmrbot.keyboards import (CATEGORIES, category_menu, confirm_remove, list_actions, main_menu, parse_callback, period_buttons,
                              player_actions, players_picker, stats_tabs)
from aiogram.types import InlineKeyboardButton

from mmrbot.storage import Player


def _p(i, name):
    return Player(i, 1, 1000 + i, name, None, 0, 0, None, None, None)


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _all_menu_buttons():
    """Кнопки главного меню и всех его категорий (подменю)."""
    buttons = _flat(main_menu())
    for key in CATEGORIES:
        buttons += _flat(category_menu(key))
    return buttons


def test_main_menu_has_actions_and_short_callbacks():
    buttons = _all_menu_buttons()
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


def test_main_menu_has_player_management_buttons():
    data = {b.callback_data for b in _all_menu_buttons()}
    assert {"m:add", "m:remove", "m:setmmr"} <= data


def test_confirm_remove_encodes_account_and_cancel():
    buttons = _flat(confirm_remove(1001))
    assert buttons[0].callback_data == "pp:rmyes:1001"
    assert "m:menu" in {b.callback_data for b in buttons}


def test_menu_has_hero_search_and_match():
    data = {b.callback_data for b in _all_menu_buttons()}
    assert {"m:hero", "m:match"} <= data


def test_period_buttons_switch_between_heroes_and_roles():
    data = [b.callback_data for b in _flat(period_buttons("hp", 1001, "week"))]
    assert "rp:1001:week" in data
    data = [b.callback_data for b in _flat(period_buttons("rp", 1001, "all"))]
    assert "hp:1001:all" in data


def test_stats_tabs_mark_current():
    buttons = _flat(stats_tabs("today"))
    assert [b.callback_data for b in buttons[:4]] == ["m:stats", "m:today", "m:week", "m:month"]
    assert buttons[1].text.startswith("•")


def test_player_actions_and_list_actions():
    data = {b.callback_data for b in _flat(player_actions(1001))}
    assert {"pp:heroes:1001", "pp:roles:1001", "pp:steam:1001", "pp:achv:1001", "m:menu"} <= data
    data = {b.callback_data for b in _flat(list_actions())}
    assert {"m:add", "m:setmmr", "m:remove", "m:menu"} <= data


def test_picker_extra_rows_before_nav():
    extra = [[InlineKeyboardButton(text="X", callback_data="m:hero")]]
    buttons = _flat(players_picker([_p(1, "Вася")], "match", extra=extra))
    assert [b.callback_data for b in buttons] == ["pp:match:1001", "m:hero", "m:menu", "x:close"]


def test_main_menu_is_compact_with_categories():
    buttons = _flat(main_menu())
    data = {b.callback_data for b in buttons}
    assert {f"m:c:{key}" for key in CATEGORIES} <= data
    assert {"m:settings", "m:help"} <= data and "x:close" not in data
    assert len(buttons) <= 9  # раньше было ~20 кнопок


def test_category_menu_has_back_and_short_callbacks():
    for key in CATEGORIES:
        buttons = _flat(category_menu(key))
        data = {b.callback_data for b in buttons}
        assert {"m:menu", "x:close"} <= data
        assert len(buttons) > 3
        assert all(len(d.encode()) <= 64 for d in data)


def test_every_action_is_in_exactly_one_place():
    data = [b.callback_data for b in _all_menu_buttons() if b.callback_data.startswith("m:")
            and not b.callback_data.startswith("m:c:") and b.callback_data not in {"m:menu"}]
    assert len(data) == len(set(data))
