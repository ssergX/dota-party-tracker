from mmrbot.ranks import rank_emoji, rank_label


def test_herald_one():
    assert rank_label(11) == "Herald 1"


def test_herald_five():
    assert rank_label(15) == "Herald 5"


def test_legend_five():
    assert rank_label(55) == "Legend 5"


def test_divine_five():
    assert rank_label(75) == "Divine 5"


def test_ancient_three():
    assert rank_label(63) == "Ancient 3"


def test_immortal_no_leaderboard():
    assert rank_label(80) == "Immortal"


def test_immortal_ignores_star_digit():
    # Immortal реально всегда 80, но не должны падать на 8x
    assert rank_label(81) == "Immortal"


def test_immortal_with_leaderboard_rank():
    assert rank_label(80, leaderboard_rank=123) == "Immortal #123"


def test_none_is_uncalibrated():
    assert rank_label(None) == "Без ранга"


def test_zero_is_uncalibrated():
    assert rank_label(0) == "Без ранга"


# --- rank_emoji ---------------------------------------------------------

def test_emoji_immortal():
    assert rank_emoji(80) == "🔱"


def test_emoji_divine():
    assert rank_emoji(75) == "💎"


def test_emoji_ancient():
    assert rank_emoji(63) == "🟣"


def test_emoji_none_is_empty():
    assert rank_emoji(None) == ""
    assert rank_emoji(0) == ""


def test_mmr_rank_mismatch_detects_drift():
    from mmrbot.ranks import mmr_rank_mismatch, rank_mmr_range
    assert rank_mmr_range(None) is None and rank_mmr_range(0) is None
    assert mmr_rank_mismatch(None, 55) is False and mmr_rank_mismatch(3200, None) is False
    assert mmr_rank_mismatch(3300, 52) is False        # Legend 2 ≈ 3234–3388
    assert mmr_rank_mismatch(4500, 52) is True         # оценка ушла на пол-медали вверх
    assert mmr_rank_mismatch(2000, 52) is True
    assert mmr_rank_mismatch(5900, 81) is False        # Immortal — только нижняя граница
    assert mmr_rank_mismatch(4000, 81) is True
