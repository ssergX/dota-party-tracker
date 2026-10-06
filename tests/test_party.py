from mmrbot.party import best_duo, together_summary


def wm(match_id, slot, radiant_win):
    return {"match_id": match_id, "player_slot": slot, "radiant_win": radiant_win}


def win(match_id):
    return wm(match_id, 0, True)  # Radiant победа


def loss(match_id):
    return wm(match_id, 0, False)  # Radiant поражение


def test_together_summary_counts_shared_matches():
    players = [
        ("Alice", [win(1), loss(2), win(3)]),
        ("Bob", [win(1), loss(2), win(4)]),
        ("Carol", [win(1)]),
    ]
    s = together_summary(players)
    assert s["games"] == 2   # матчи 1 и 2 (у >=2 игроков)
    assert s["wins"] == 1    # матч 1 победа
    assert s["losses"] == 1  # матч 2 поражение


def test_together_summary_skips_opposite_teams():
    players = [
        ("Alice", [win(5)]),                 # Radiant, победа
        ("Dave", [wm(5, 128, True)]),         # Dire в том же матче → поражение (разные команды)
    ]
    s = together_summary(players)
    assert s["games"] == 0  # противоположные команды не считаем совместной игрой


def test_together_summary_empty():
    assert together_summary([]) == {"games": 0, "wins": 0, "losses": 0}


def test_best_duo_picks_pair_with_most_shared_games():
    players = [
        ("Alice", [win(1), loss(2), win(3)]),
        ("Bob", [win(1), loss(2)]),
        ("Carol", [win(1)]),
    ]
    duo = best_duo(players)
    assert set(duo["pair"]) == {"Alice", "Bob"}  # 2 совместных матча
    assert duo["games"] == 2
    assert duo["wins"] == 1


def test_best_duo_none_when_no_shared():
    players = [("Alice", [win(1)]), ("Bob", [win(2)])]
    assert best_duo(players) is None


def test_best_duo_does_not_collapse_duplicate_names():
    # Два аккаунта с одинаковым именем не должны схлопываться (потеря матчей).
    players = [
        ("Alex", [win(1), win(2), win(3)]),  # аккаунт A1: 3 общих с Bob
        ("Bob", [win(1), win(2), win(3)]),
        ("Alex", [win(9)]),                  # аккаунт A2: общих нет
    ]
    duo = best_duo(players)
    assert duo is not None
    assert duo["games"] == 3  # матчи A1 не потеряны


def test_together_counts_same_side_subgroup_when_split():
    # A,B на Radiant (победа), C на Dire — A и B сыграли вместе на одной стороне.
    players = [
        ("A", [wm(50, 0, True)]),
        ("B", [wm(50, 1, True)]),
        ("C", [wm(50, 128, True)]),
    ]
    s = together_summary(players)
    assert s["games"] == 1
    assert s["wins"] == 1


def test_together_skips_even_split():
    # 2 на 2 — неоднозначно, не считаем.
    players = [
        ("A", [wm(60, 0, True)]),
        ("B", [wm(60, 1, True)]),
        ("C", [wm(60, 128, True)]),
        ("D", [wm(60, 129, True)]),
    ]
    assert together_summary(players)["games"] == 0


def test_together_ignores_games_where_a_player_was_solo():
    """Двое из чата на одной стороне, но один из них в соло-лобби — это совпадение, а не совместная игра."""
    solo = {**win(1), "party_size": 1}
    duo = {**win(2), "party_size": 2}
    unknown = win(3)
    players = [("Alice", [solo, duo, unknown]), ("Bob", [{**win(1), "party_size": 2}, {**win(2), "party_size": 2}, win(3)])]
    assert together_summary(players)["games"] == 2          # матч 1 не считается; 2 (пати) и 3 (размер неизвестен) — да
    assert best_duo(players)["games"] == 2
