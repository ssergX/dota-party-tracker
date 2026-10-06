import pytest

from mmrbot.heroes import HERO_NAMES, RU_ALIASES, find_hero, hero_name


@pytest.mark.parametrize("query, expected", [
    ("морф", "Morphling"), ("морфлинг", "Morphling"), ("Моча", "Morphling"), ("morph", "Morphling"),
    ("джага", "Juggernaut"), ("джагер", "Juggernaut"), ("ДЖАГЕРНАУТ", "Juggernaut"), ("jugg", "Juggernaut"),
    ("антимаг", "Anti-Mage"), ("анти-маг", "Anti-Mage"), ("анти маг", "Anti-Mage"), ("ам", "Anti-Mage"),
    ("пудж", "Pudge"), ("пуджа", "Pudge"),
    ("дроу", "Drow Ranger"), ("снайпер", "Sniper"), ("инвокер", "Invoker"), ("карл", "Invoker"),
    ("рубик", "Rubick"), ("сларк", "Slark"), ("рыба", "Slark"),
    ("кристалка", "Crystal Maiden"), ("цм", "Crystal Maiden"),
    ("фантомка", "Phantom Assassin"), ("па", "Phantom Assassin"),
    ("бристл", "Bristleback"), ("кабан", "Bristleback"),
    ("шейкер", "Earthshaker"), ("эмбер", "Ember Spirit"),
    ("квоп", "Queen of Pain"), ("нат профет", "Nature's Prophet"), ("фурион", "Nature's Prophet"),
    ("axe", "Axe"), ("Anti", "Anti-Mage"), ("ls", "Lifestealer"),
])
def test_find_hero_russian_and_slang(query, expected):
    assert hero_name(find_hero(query)) == expected


def test_unknown_hero_is_none():
    assert find_hero("несуществующий") is None
    assert find_hero("") is None
    assert find_hero("   ") is None


def test_every_hero_has_russian_aliases():
    missing = [name for hid, name in HERO_NAMES.items() if hid not in RU_ALIASES]
    assert not missing, f"нет русских алиасов: {missing}"
    assert set(RU_ALIASES) <= set(HERO_NAMES)


def test_aliases_do_not_collide_between_heroes():
    seen = {}
    for hid, aliases in RU_ALIASES.items():
        for alias in aliases.split(","):
            key = alias.strip().lower().replace("ё", "е").replace(" ", "").replace("-", "")
            assert key, f"пустой алиас у {hid}"
            assert key not in seen or seen[key] == hid, f"«{alias}» у героев {seen[key]} и {hid}"
            seen[key] = hid


def test_every_alias_resolves_to_own_hero():
    for hid, aliases in RU_ALIASES.items():
        for alias in aliases.split(","):
            assert find_hero(alias) == hid, f"«{alias}» → {find_hero(alias)}, ожидался {hid}"
