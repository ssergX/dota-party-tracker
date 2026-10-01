import pytest

from mmrbot.commands import parse_add_args, parse_hour, parse_name_and_mmr, parse_step


# --- /add ---------------------------------------------------------------

def test_add_identifier_name_mmr():
    ident, name, mmr = parse_add_args("dotabuff.com/players/123 Вася 5400")
    assert ident == "dotabuff.com/players/123"
    assert name == "Вася"
    assert mmr == 5400


def test_add_identifier_only():
    ident, name, mmr = parse_add_args("123456")
    assert ident == "123456"
    assert name is None
    assert mmr is None


def test_add_identifier_and_name_no_mmr():
    ident, name, mmr = parse_add_args("123456 Вася")
    assert name == "Вася"
    assert mmr is None


def test_add_identifier_and_mmr_no_name():
    ident, name, mmr = parse_add_args("123456 5400")
    assert name is None
    assert mmr == 5400


def test_add_multiword_name():
    ident, name, mmr = parse_add_args("123456 Длинное Имя 5400")
    assert name == "Длинное Имя"
    assert mmr == 5400


def test_add_empty_raises():
    with pytest.raises(ValueError):
        parse_add_args("")


def test_add_absurd_mmr_raises():
    with pytest.raises(ValueError):
        parse_add_args("123456 Вася 99999999")


def test_add_negative_mmr_raises():
    with pytest.raises(ValueError):
        parse_add_args("123456 Вася -5")


def test_add_mmr_upper_boundary_ok():
    _, _, mmr = parse_add_args("123456 Вася 20000")
    assert mmr == 20000


# --- /setmmr ------------------------------------------------------------

def test_setmmr_name_and_value():
    name, mmr = parse_name_and_mmr("Вася 5300")
    assert name == "Вася"
    assert mmr == 5300


def test_setmmr_multiword_name():
    name, mmr = parse_name_and_mmr("Длинное Имя 5300")
    assert name == "Длинное Имя"
    assert mmr == 5300


def test_setmmr_missing_value_raises():
    with pytest.raises(ValueError):
        parse_name_and_mmr("Вася")


def test_setmmr_absurd_value_raises():
    with pytest.raises(ValueError):
        parse_name_and_mmr("Вася 99999999")


def test_setmmr_negative_value_raises():
    with pytest.raises(ValueError):
        parse_name_and_mmr("Вася -100")


def test_setmmr_empty_raises():
    with pytest.raises(ValueError):
        parse_name_and_mmr("")


# --- /setstep -----------------------------------------------------------

def test_step_ok():
    assert parse_step("30") == 30


def test_step_zero_raises():
    with pytest.raises(ValueError):
        parse_step("0")


def test_step_non_numeric_raises():
    with pytest.raises(ValueError):
        parse_step("abc")


# --- /settime -----------------------------------------------------------

def test_hour_ok():
    assert parse_hour("8") == 8


def test_hour_out_of_range_raises():
    with pytest.raises(ValueError):
        parse_hour("24")


def test_hour_negative_raises():
    with pytest.raises(ValueError):
        parse_hour("-1")


def test_parse_target_period():
    from mmrbot.commands import parse_target_period
    assert parse_target_period("") == (None, "all")
    assert parse_target_period("месяц") == (None, "month")
    assert parse_target_period("@vasya") == ("vasya", "all")
    assert parse_target_period("@Vasya неделя") == ("Vasya", "week")
    assert parse_target_period("день Вася Пупкин") == ("Вася Пупкин", "day")
    assert parse_target_period("week") == (None, "week")


def test_parse_match_args():
    from mmrbot.commands import parse_match_args
    assert parse_match_args("") == (None, None)
    assert parse_match_args("последний") == (None, None)
    assert parse_match_args("8138048280") == (8138048280, None)
    assert parse_match_args("@vasya") == (None, "vasya")
    assert parse_match_args("8138048280 @vasya") == (8138048280, "vasya")
    assert parse_match_args("Вася последний") == (None, "Вася")
