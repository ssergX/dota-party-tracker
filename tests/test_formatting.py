from mmrbot.formatting import (
    format_delta,
    plural_games,
    render_awards,
    render_compare_table,
    render_heroes,
    render_leaderboard,
    render_player_card,
    render_together,
    standing_line,
)
from mmrbot.tracker import PlayerSummary, build_chat_comparison


def summary(**kw):
    base = dict(
        display_name="Вася",
        account_id=42,
        rank="Divine 5",
        anchor_mmr=5000,
        current_mmr=5050,
        mmr_delta=50,
        games_total=4,
        wins_total=3,
        losses_total=1,
        winrate=0.75,
        kda_ratio=4.2,
        avg_kills=8.0,
        avg_deaths=4.0,
        avg_assists=7.0,
        games_today=2,
        wins_today=2,
        losses_today=0,
        delta_today=50,
    )
    base.update(kw)
    return PlayerSummary(**base)


# --- format_delta -------------------------------------------------------

def test_format_delta_positive():
    assert format_delta(50) == "+50"


def test_format_delta_negative():
    assert format_delta(-100) == "-100"


def test_format_delta_zero():
    assert format_delta(0) == "0"


# --- render_leaderboard -------------------------------------------------

def test_leaderboard_empty_gives_hint():
    text = render_leaderboard([])
    assert "/add" in text


def test_leaderboard_shows_name_rank_mmr_and_estimate_marker():
    text = render_leaderboard([summary()])
    assert "Вася" in text
    assert "Divine 5" in text
    assert "5050" in text
    assert "+50" in text
    assert "≈" in text  # оценка помечена
    assert "75%" in text


def test_leaderboard_orders_and_medals_players():
    text = render_leaderboard([summary(display_name="Aaa"), summary(display_name="Bbb")])
    assert text.index("Aaa") < text.index("Bbb")
    assert "🥇" in text and "🥈" in text


# --- новый дизайн (карточки) --------------------------------------------

def test_plural_games():
    assert plural_games(1) == "1 игра"
    assert plural_games(2) == "2 игры"
    assert plural_games(5) == "5 игр"
    assert plural_games(11) == "11 игр"
    assert plural_games(21) == "21 игра"


def test_zero_games_collapsed_no_noise():
    text = render_leaderboard([summary(games_total=0, wins_total=0, losses_total=0, winrate=0.0, kda_ratio=0.0)])
    assert "Игр пока нет" in text
    assert "KDA 0.00" not in text
    assert "0–0" not in text


def test_leaderboard_hides_avg_breakdown():
    # Разбивку K/D/A показываем только в /player, не в лидерборде.
    text = render_leaderboard([summary(avg_kills=8.0, avg_deaths=4.0, avg_assists=7.0)])
    assert "8.0/4.0/7.0" not in text


def test_positive_delta_shows_up_arrow():
    text = render_leaderboard([summary(mmr_delta=50, games_total=4)])
    assert "📈" in text


def test_negative_delta_shows_down_arrow():
    text = render_leaderboard([summary(current_mmr=4900, mmr_delta=-100, games_total=4)])
    assert "📉" in text
    assert "-100" in text


def test_name_is_html_escaped_and_bold():
    text = render_leaderboard([summary(display_name="A<b>&")])
    assert "A&lt;b&gt;&amp;" in text  # экранировано
    assert "<b>" in text              # жирный присутствует


def test_leaderboard_negative_delta_shows_minus():
    text = render_leaderboard([summary(current_mmr=4900, mmr_delta=-100)])
    assert "-100" in text
    assert "4900" in text


def test_today_view_marks_no_games():
    text = render_leaderboard([summary(games_today=0, wins_today=0, losses_today=0, delta_today=0)], today_only=True)
    assert "Вася" in text
    # без игр сегодня — не показываем ложную дельту, а пишем про отсутствие игр
    assert "игр не было" in text.lower()


def test_player_without_anchor_mmr_shows_question_not_crash():
    text = render_leaderboard([summary(anchor_mmr=None, current_mmr=None, mmr_delta=25)])
    assert "Вася" in text
    assert "+25" in text


def test_leaderboard_shows_win_streak():
    text = render_leaderboard([summary(streak_type="W", streak_len=3)])
    assert "🔥" in text
    assert "3" in text


def test_leaderboard_shows_perf_in_line():
    text = render_leaderboard([summary(avg_perf=0.58, games_total=10)])
    assert "перф 58" in text


# --- awards -------------------------------------------------------------

def test_render_awards_lists_leaders_with_period_label():
    awards = [{"key": "winrate", "emoji": "👑", "title": "Наивысший винрейт", "player": "A", "detail": "80%"}]
    text = render_awards(awards, "за сутки")
    assert "Награды за сутки" in text and "A" in text and "80%" in text


def test_render_awards_empty_when_no_awards():
    assert render_awards([]) == ""


# --- together -----------------------------------------------------------

def test_render_together_with_shared_games():
    result = {"player_count": 2, "summary": {"games": 5, "wins": 3, "losses": 2},
              "duo": {"pair": ("Alice", "Bob"), "games": 4, "wins": 3, "winrate": 0.75}}
    text = render_together(result)
    assert "5" in text
    assert "Alice" in text and "Bob" in text


def test_render_together_no_games():
    result = {"player_count": 2, "summary": {"games": 0, "wins": 0, "losses": 0}, "duo": None}
    text = render_together(result)
    assert "нет" in text.lower() or "совмест" in text.lower()


# --- heroes -------------------------------------------------------------

def test_render_heroes_shows_hero_names():
    s = summary(display_name="Вася", top_heroes=[{"hero_id": 1, "games": 5, "wins": 3, "winrate": 0.6}])
    text = render_heroes([s])
    assert "Вася" in text
    assert "Anti-Mage" in text  # hero_id 1


# --- player card --------------------------------------------------------

def test_render_player_card_is_windowed_only():
    # Карточка показывает только окно (последние игры), без карьерных линий/распределений.
    s = summary(
        display_name="Вася",
        avg_gpm_window=520.0, avg_net_worth_window=18000.0, avg_hero_damage_window=22000.0,
        solo=(10, 6), party=(5, 4),
        top_heroes=[{"hero_id": 8, "games": 4, "wins": 3, "winrate": 0.75}],
        lanes={2: (10, 6), 0: (5, 2)},   # карьерное — НЕ должно попасть
        gpm_median=999.0,                # карьерное — НЕ должно попасть
    )
    text = render_player_card(s)
    assert "Вася" in text
    assert "520" in text            # windowed GPM
    assert "Juggernaut" in text     # hero_id 8
    assert "соло" in text.lower()
    assert "последние" in text      # явная пометка окна
    # карьерных строк быть не должно
    assert "Mid" not in text
    assert "без линии" not in text
    assert "999" not in text


def test_render_player_card_windowed_records():
    s = summary(
        display_name="Вася",
        recent_form=[True, False, True],
        best_game={"kills": 10, "deaths": 1, "assists": 10, "hero_id": 8, "kda": 20.0},
        longest_win_streak=3,
    )
    text = render_player_card(s)
    assert "🟢🔴🟢" in text                    # форма
    assert "10/1/10" in text                    # лучшая игра
    assert "3" in text                          # макс серия


def test_render_player_card_shows_perf_score():
    s = summary(avg_perf=0.72, enriched_games=9, avg_hero_damage_window=22000.0, avg_net_worth_window=18000.0)
    text = render_player_card(s)
    assert "72/100" in text
    assert "перф" in text.lower()


def test_render_player_card_shows_new_metrics():
    s = summary(
        lobby_rank=74,  # Divine 4
        hero_pool=8,
        wins_losses={"win": {"games": 6, "avg_deaths": 5.0, "avg_kda": 4.2},
                     "loss": {"games": 4, "avg_deaths": 11.0, "avg_kda": 1.3}},
    )
    text = render_player_card(s)
    assert "лобби" in text.lower()
    assert "Divine" in text            # сложность лобби 74 → Divine 4
    assert "победах" in text and "поражениях" in text
    assert "8" in text                 # пул героев


def test_render_player_card_shows_skill_breakdown_and_role():
    s = summary(
        role_style="кор (фарм)",
        skill={"gold_per_min": 0.45, "hero_damage_per_min": 0.78, "kills_per_min": 0.66, "hero_healing_per_min": 0.9},
    )
    text = render_player_card(s)
    assert "Профиль навыков" in text
    assert "Фарм" in text            # категория
    assert "78%" in text             # урон-перцентиль
    assert ("▰" in text) or ("▱" in text)  # бар
    assert "игровая роль" in text.lower() and "кор" in text.lower()


def test_render_player_card_with_standing_block():
    s = summary()
    text = render_player_card(s, standing="📊 В чате (из 3): сила #1 · перф #1")
    assert "В чате" in text
    assert "сила #1" in text


# --- сравнение в чате ---------------------------------------------------

def _two_player_comparison():
    a = summary(display_name="A", avg_perf=0.8, winrate=0.6, kda_ratio=4.0, avg_gpm_window=500.0, enriched_games=9, detail_games=9)
    b = summary(display_name="B", avg_perf=0.4, winrate=0.4, kda_ratio=2.0, avg_gpm_window=400.0, enriched_games=9, detail_games=9)
    return build_chat_comparison([a, b]), [a, b]


def test_render_compare_table_orders_by_power():
    comp, summaries = _two_player_comparison()
    text = render_compare_table(comp, summaries)
    assert "Сравнение игроков" in text
    assert text.index("A") < text.index("B")   # A первым (сильнее)
    assert "🥇" in text and "🥈" in text


def test_standing_line_shows_ranks():
    comp, _ = _two_player_comparison()
    line = standing_line(comp, "A")
    assert "сила #1" in line
    assert "перф #1" in line


def test_standing_line_none_when_alone():
    a = summary(display_name="Solo", avg_perf=0.5)
    comp = build_chat_comparison([a])
    assert standing_line(comp, "Solo") is None  # сравнивать не с кем


def test_find_hero_by_name_and_alias():
    from mmrbot.heroes import find_hero
    assert find_hero("axe") == 2
    assert find_hero("Anti") == 1
    assert find_hero("  necro ") == 36
    assert find_hero("несуществующий") is None


def test_render_player_heroes_and_roles():
    from mmrbot.formatting import render_player_heroes, render_roles
    rows = [{"hero_id": 2, "games": 4, "wins": 3, "losses": 1, "winrate": 0.75, "kda": 3.5,
             "avg_imp": 6.4, "avg_gpm": 500.0}]
    text = render_player_heroes("Вася <b>", "month", rows)
    assert "Вася &lt;b&gt;" in text and "Axe" in text and "75%" in text and "+6" in text
    assert "месяц" in text
    assert "игр нет" in render_player_heroes("Вася", "day", []).lower()
    roles = render_roles("Вася", [{"position": 1, "games": 5, "wins": 3, "losses": 2,
                                   "winrate": 0.6, "kda": 2.0, "avg_imp": None, "avg_gpm": None}])
    assert "Pos 1" in roles or "Керри" in roles


def test_render_match_card_and_hero_detail():
    from mmrbot.formatting import render_hero_detail, render_match_card
    from mmrbot.storage import Player
    p = Player(1, 1, 1, "Вася", None, 0, 0, None, None, None)
    row = {"match_id": 11, "start_time": 1_700_000_000, "hero_id": 2, "player_slot": 0, "radiant_win": True,
           "kills": 10, "deaths": 2, "assists": 5, "duration": 2400, "gpm": 600, "xpm": 700,
           "net_worth": 30000, "hero_damage": 25000, "tower_damage": 3000, "hero_healing": 0,
           "last_hits": 300, "denies": 10, "level": 25, "position": 1, "role": "CORE",
           "lane": "SAFE_LANE", "imp": 12}
    text = render_match_card({"player": p, "match": row})
    for needle in ("Вася", "Axe", "10/2/5", "Победа", "Pos 1", "+12", "11"):
        assert needle in text
    s = {"games": 3, "wins": 2, "losses": 1, "winrate": 2 / 3, "kda": 3.0, "avg_imp": 4.0, "avg_gpm": 500.0}
    text = render_hero_detail(2, "all", [(p, s)])
    assert "Axe" in text and "Вася" in text and "67%" in text
    assert "не играли" in render_hero_detail(2, "all", []).lower()


def _fp(acc, radiant, hero, pos, name, imp=5):
    return {"account_id": acc, "name": name, "is_radiant": radiant, "hero_id": hero, "position": pos,
            "role": "CORE", "lane": "SAFE_LANE", "kills": 3, "deaths": 1, "assists": 4, "imp": imp,
            "gpm": 500, "xpm": 600, "net_worth": 20000, "hero_damage": 15000, "tower_damage": 1000,
            "hero_healing": 0, "last_hits": 200, "denies": 5, "level": 22}


def test_render_full_match_marks_tracked_and_shows_both_teams():
    from mmrbot.formatting import render_full_match
    match = {"match_id": 77, "start_time": 1_700_000_000, "duration": 2400, "radiant_win": False,
             "players": [_fp(1, True, 2, 1, "Steam1"), _fp(2, False, 5, 5, None, imp=None),
                         _fp(3, False, 1, 2, "Чужой")]}
    text = render_full_match(match, {1: "Вася"}, focus=1)
    assert "Матч 77" in text and "Победитель: Dire" in text
    assert "★ <b>Вася</b>" in text          # свой игрок подсвечен и назван по имени из бота
    assert "Radiant" in text and "Dire" in text
    assert "Чужой" in text and "Crystal Maiden" in text and "IMP —" in text
    assert "GPM 500" in text                  # подробная карточка фокус-игрока сверху
    assert "Пати" not in text
    no_focus = render_full_match(match, {}, focus=None)
    assert "GPM" not in no_focus and "Матч 77" in no_focus


def test_imp_formatting_has_no_negative_zero():
    from mmrbot.formatting import _imp
    assert _imp(-0.4) == "0" and _imp(0.2) == "0" and _imp(-9.3) == "-9" and _imp(4.6) == "+5"
    assert _imp(None) == "—"


def test_render_period_leaderboard():
    from mmrbot.formatting import render_period_leaderboard

    rows = [
        {"name": "Аня", "games": 5, "wins": 4, "losses": 1, "delta": 75, "winrate": 0.8, "kda": 3.2},
        {"name": "Боря", "games": 2, "wins": 0, "losses": 2, "delta": -50, "winrate": 0.0, "kda": 1.1},
        {"name": "Ваня", "games": 0, "wins": 0, "losses": 0, "delta": 0, "winrate": 0.0, "kda": 0.0},
    ]
    text = render_period_leaderboard(rows, "week")
    assert "за неделю" in text
    assert "Аня" in text and "4–1" in text and "80%" in text
    assert "📈" in text and "📉" in text
    assert "игр не было" in text  # Ваня
    assert render_period_leaderboard([], "week").startswith("Пока пусто")


def test_find_hero_alias_ls_is_lifestealer():
    from mmrbot.heroes import find_hero, hero_name

    assert hero_name(find_hero("ls")) == "Lifestealer"
    assert hero_name(find_hero("axe")) == "Axe"


def test_match_card_ignores_unknown_party_key():
    from types import SimpleNamespace
    from mmrbot.formatting import render_match_card

    row = {"match_id": 1, "start_time": 1_700_000_000, "player_slot": 0, "radiant_win": True,
           "hero_id": 1, "kills": 1, "deaths": 1, "assists": 1}
    text = render_match_card({"player": SimpleNamespace(display_name="Вася"), "match": row, "party": [("x", row)]})
    assert "Матч 1" in text


def test_player_heroes_block_layout_with_emoji():
    from mmrbot.formatting import render_player_heroes
    rows = [{"hero_id": 1, "games": 987, "winrate": 0.53, "kda": 4.4, "avg_imp": -1, "avg_gpm": 624}]
    text = render_player_heroes("Shinoame", "all", rows)
    assert text.startswith("🦸 <b>Герои: Shinoame</b>")
    assert "🥇" in text and "987 игр" in text
    assert "🟡 53%" in text and "⚔️ KDA 4.4" in text and "📊 IMP -1" in text and "💰 GPM 624" in text


def test_roles_block_has_position_emoji_and_wr_dot():
    from mmrbot.formatting import render_roles
    rows = [{"position": 5, "games": 482, "winrate": 0.58, "kda": 2.6, "avg_imp": 1, "avg_gpm": 369}]
    text = render_roles("Shinoame", rows)
    assert "💚 <b>Pos 5 · Фулл-саппорт</b>" in text
    assert "🟢 58%" in text


def test_player_roster_shows_current_mmr_and_extra_info():
    from mmrbot.formatting import render_player_list
    text = render_player_list([summary(streak_type="W", streak_len=3)])
    assert "👥" in text and "Вася" in text
    assert "≈ 5050 MMR" in text and "старт 5000" in text and "+50" in text
    assert "4 игры" in text and "3–1" in text and "75%" in text
    assert "3 подряд" in text and "id " in text


def test_player_roster_without_mmr_or_games():
    from mmrbot.formatting import render_player_list
    text = render_player_list([summary(current_mmr=None, anchor_mmr=None, games_total=0, mmr_delta=0)])
    assert "MMR не указан" in text and "Игр пока нет" in text


def test_player_roster_empty_gives_hint():
    from mmrbot.formatting import render_player_list
    assert "/add" in render_player_list([])


def test_find_hero_no_random_substring_and_russian_names():
    from mmrbot.heroes import find_hero, hero_name
    assert find_hero("od") is None                       # «od» сидит внутри Bloodseeker — это не поиск
    assert hero_name(find_hero("пудж")) == "Pudge"
    assert hero_name(find_hero("Джаггернаут")) == "Juggernaut"
    assert hero_name(find_hero("вк")) == "Wraith King"
    assert hero_name(find_hero("wind")) == "Windranger"
    assert hero_name(find_hero("seeker")) == "Bloodseeker"  # от 4 символов подстрока работает


def test_update_heroes_adds_new_hero_and_renames():
    from mmrbot import heroes
    try:
        added = heroes.update_heroes([{"id": 9999, "localized_name": "Новый Герой"}, {"id": "x"}, {"id": 2, "localized_name": "Axe"}])
        assert added == 1 and heroes.hero_name(9999) == "Новый Герой"
        assert heroes.hero_name(2) == "Axe"
        assert heroes.update_heroes(None) == 0
    finally:
        heroes.HERO_NAMES.pop(9999, None)


def test_card_labels_do_not_overclaim():
    text = render_player_card(summary(
        games_total=40, enriched_games=6, detail_games=9, avg_perf=0.6, avg_gpm_window=500.0,
    ))
    assert "Вся ранкед-история" in text and "Период анализа" not in text
    assert "по 9 из 40" in text                  # экономика усреднена не по всем играм
    assert "по 6 из 40" in text                  # и перф тоже


def test_steam_profile_does_not_claim_last_dota_login():
    from mmrbot.formatting import render_steam_profile
    import inspect
    sig = inspect.signature(render_steam_profile)
    kwargs = {n: None for n in sig.parameters}
    profile = {"personaname": "x", "last_login": "2015-01-01T00:00:00.000Z"}
    # аргументы у функции свои — подставим по именам
    kwargs.update({"profile": profile, "account_id": 1, "rank": "Divine 1"})
    text = render_steam_profile(**{k: v for k, v in kwargs.items() if k in sig.parameters})
    assert "Последний вход" not in text and "2015" not in text


def test_card_shows_unknown_party_and_mmr_drift_hint():
    text = render_player_card(summary(party_unknown=(3, 1), mmr_drift=True))
    assert "размер пати неизвестен" in text and "3 игры" in text
    assert "/setmmr" in text


def test_skill_block_damage_group_uses_tower_damage_benchmark():
    from mmrbot.formatting import _skill_block
    text = _skill_block({"hero_damage_per_min": 0.5, "tower_damage": 0.9})
    assert "Урон" in text and "70%" in text      # среднее героев и строений, а не только героев


def test_leaderboard_explains_mmr_period_when_it_differs():
    from mmrbot.formatting import render_leaderboard
    assert "за 3 игры с момента задания MMR" in render_leaderboard([summary(games_total=10, anchor_games=3)])
    assert "с момента задания MMR" not in render_leaderboard([summary(games_total=4, anchor_games=4)])
