"""Рендер сообщений бота — карточный дизайн, Telegram HTML.

Сообщения-борды (лидерборд, награды, совместка, герои, карточка игрока, список)
отправляются с parse_mode=HTML — имена экранируются через html.escape.
Обычные подтверждения/ошибки шлются обычным текстом (без разметки).
"""
from __future__ import annotations

import html
from typing import Optional

from mmrbot.heroes import hero_name
from mmrbot.ranks import rank_label
from mmrbot.tracker import PlayerSummary, compute_awards

POSITIONS = {1: "🥇", 2: "🥈", 3: "🥉"}
LANE_NAMES = {1: "Safe", 2: "Mid", 3: "Off", 4: "Лес"}


def _esc(text) -> str:
    return html.escape(str(text))


def _b(text) -> str:
    """Жирный с экранированием сырого текста."""
    return f"<b>{_esc(text)}</b>"


def _pos(index: int) -> str:
    return POSITIONS.get(index, f"{index}.")


def plural_games(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = "игра"
    elif 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        word = "игры"
    else:
        word = "игр"
    return f"{n} {word}"


def plural_heroes(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "герой"
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return "героя"
    return "героев"


def dotabuff_player_url(account_id: int) -> str:
    return f"https://www.dotabuff.com/players/{account_id}"


def dotabuff_match_url(match_id: int) -> str:
    return f"https://www.dotabuff.com/matches/{match_id}"


def format_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta}"
    if delta < 0:
        return str(delta)  # уже с минусом
    return "0"


def _trend(delta: int) -> str:
    if delta > 0:
        return f"  📈{format_delta(delta)}"
    if delta < 0:
        return f"  📉{format_delta(delta)}"
    return ""


def _today_delta(delta: int) -> str:
    if delta > 0:
        return f"📈{format_delta(delta)}"
    if delta < 0:
        return f"📉{format_delta(delta)}"
    return "±0"


def _mmr_str(current: Optional[int]) -> str:
    return f"≈ {current} MMR" if current is not None else "≈ ? MMR"


def _rank_with_emoji(s: PlayerSummary) -> str:
    prefix = f"{s.rank_emoji} " if s.rank_emoji else ""
    return f"{prefix}{s.rank}"


def _streak_str(s: PlayerSummary) -> str:
    if s.streak_len < 2:
        return ""
    return f"  🔥{s.streak_len} подряд" if s.streak_type == "W" else f"  💧{s.streak_len} подряд"


def _heroes_line(top_heroes: list[dict]) -> str:
    if not top_heroes:
        return "нет данных"
    return ", ".join(
        f"{_esc(hero_name(h['hero_id']))} ({h['games']}и, {h['winrate'] * 100:.0f}%)" for h in top_heroes
    )


def _fmt_wr(games: int, wins: int) -> str:
    if not games:
        return "—"
    return f"{wins}–{games - wins} ({wins / games * 100:.0f}%)"


# --- лидерборд ----------------------------------------------------------

def _card_full(index: int, s: PlayerSummary) -> str:
    lines = [f"{_pos(index)} {_b(s.display_name)}"]
    lines.append(f"    {_rank_with_emoji(s)} · {_b(_mmr_str(s.current_mmr))}{_trend(s.mmr_delta)}")
    if s.games_total == 0:
        lines.append("    💤 игры отсутствуют")
    else:
        perf = f" · перф {s.avg_perf * 100:.0f}" if s.avg_perf is not None else ""
        lines.append(
            f"    🎮 {plural_games(s.games_total)} · {s.wins_total}–{s.losses_total} "
            f"({s.winrate * 100:.0f}%) · KDA {s.kda_ratio:.2f}{perf}{_streak_str(s)}"
        )
        if s.games_today:
            lines.append(
                f"    📅 сегодня: {plural_games(s.games_today)}, "
                f"{_today_delta(s.delta_today)} ({s.wins_today}–{s.losses_today})"
            )
    return "\n".join(lines)


def _card_today(index: int, s: PlayerSummary) -> str:
    lines = [f"{_pos(index)} {_b(s.display_name)} · {_rank_with_emoji(s)}"]
    if s.games_today == 0:
        lines.append("    💤 сегодня игр не было")
    else:
        lines.append(
            f"    📅 сегодня: {plural_games(s.games_today)}, "
            f"{_today_delta(s.delta_today)} ({s.wins_today}–{s.losses_today})"
        )
    return "\n".join(lines)


def render_leaderboard(summaries: list[PlayerSummary], today_only: bool = False) -> str:
    if not summaries:
        return (
            "В данном чате нет отслеживаемых игроков.\n"
            "Для добавления аккаунта используйте: /add «ссылка Dotabuff/OpenDota или ID» Имя [стартовый_MMR]\n"
            "Пример: /add dotabuff.com/players/123456 Вася 5400"
        )

    if today_only:
        header = "📅 <b>Статистика за сегодня</b> · ранкед"
        blocks = [_card_today(i, s) for i, s in enumerate(summaries, start=1)]
        return header + "\n\n" + "\n\n".join(blocks)

    header = "🏆 <b>Рейтинг участников</b>\n<i>ранкед, за всё время</i>"
    blocks = [_card_full(i, s) for i, s in enumerate(summaries, start=1)]
    return header + "\n\n" + "\n\n".join(blocks)


def render_period_leaderboard(rows: list[dict], period: str) -> str:
    """Лидерборд за неделю/месяц: победы–поражения, винрейт, оценка MMR-дельты."""
    if not rows:
        return render_leaderboard([])
    label = {"week": "за неделю", "month": "за месяц"}.get(period, "за период")
    blocks = []
    for i, r in enumerate(rows, start=1):
        head = f"{_pos(i)} {_b(r['name'])}"
        if r["games"] == 0:
            blocks.append(f"{head}\n    💤 игр не было")
            continue
        blocks.append(
            f"{head} · {_today_delta(r['delta'])}\n"
            f"    🎮 {plural_games(r['games'])} · {r['wins']}–{r['losses']} "
            f"({r['winrate'] * 100:.0f}%) · KDA {r['kda']:.2f}"
        )
    return f"📅 <b>Статистика {label}</b> · ранкед\n\n" + "\n\n".join(blocks)


# --- награды ------------------------------------------------------------

def render_awards(summaries: list[PlayerSummary]) -> str:
    awards = compute_awards(summaries)
    if not awards:
        return ""
    lines = ["🏅 <b>Отличия участников</b>"]
    for award in awards:
        lines.append(f"{award['title']} — {_b(award['player'])} ({_esc(award['detail'])})")
    lines.append("\n<i>🌟 Рекорды отдельных игр за день, неделю, месяц, год: /records</i>")
    return "\n".join(lines)


PULSE_RECORDS = ("gpm", "kills", "imp")  # какие рекорды недели выносим в «Пульс»


def _form_dots(form: list) -> str:
    return "".join("🟢" if won else "🔴" for won in form)


def render_party_pulse(summaries: list[PlayerSummary], week_rows: list[dict], week_records: dict) -> str:
    """«Пульс пати» под рейтингом: сегодня, неделя, форма игроков, рекорды недели (работает и для одного игрока)."""
    lines = ["📡 <b>Пульс пати</b>"]

    t_games = sum(s.games_today for s in summaries)
    if t_games:
        t_wins = sum(s.wins_today for s in summaries)
        t_delta = sum(s.delta_today for s in summaries)
        lines.append(f"📅 Сегодня: {plural_games(t_games)} · {_fmt_wr(t_games, t_wins)} · {_today_delta(t_delta)}")
    else:
        lines.append("📅 Сегодня: игр пока не было")

    played = [r for r in week_rows if r["games"] > 0]
    if played:
        w_games = sum(r["games"] for r in played)
        w_wins = sum(r["wins"] for r in played)
        w_delta = sum(r["delta"] for r in played)
        lines.append(f"🗓️ За неделю: {plural_games(w_games)} · {_fmt_wr(w_games, w_wins)} · {_today_delta(w_delta)}")
        if len(played) >= 2:
            best = max(played, key=lambda r: r["delta"])
            if best["delta"] > 0:
                lines.append(f"🚀 Лидер недели: {_b(best['name'])} {_today_delta(best['delta'])} ({best['wins']}–{best['losses']})")
    else:
        lines.append("🗓️ За неделю: игр не было")

    form = [s for s in summaries if s.recent_form]
    if form:
        lines.append("")
        lines.append("<b>Форма</b> <i>(последние игры, новые справа)</i>")
        for s in form:
            lines.append(f"{_b(s.display_name)}  {_form_dots(s.recent_form)}")

    picked = [r for r in week_records.get("records", []) if r["key"] in PULSE_RECORDS]
    if picked:
        lines.append("")
        lines.append("🌟 <b>Рекорды недели</b>")
        for r in picked:
            match = r["match"]
            lines.append(
                f"{r['emoji']} {_esc(r['text'])} — {_b(r['player'])} · {_esc(hero_name(match.get('hero_id')))} · "
                f'<a href="{dotabuff_match_url(match["match_id"])}">матч</a>'
            )
        lines.append("<i>Все рекорды за день/месяц/год — /records</i>")
    return "\n".join(lines)


# --- совместная игра ----------------------------------------------------

def render_together(result: dict) -> str:
    summary = result.get("summary", {})
    games = summary.get("games", 0)
    if games == 0:
        return (
            "🤝 <b>Совместные игры</b>\n\n"
            "😴 Совместные ранкед-игры не обнаружены (возможно, данные ещё загружаются).\n"
            "После первой совместной игры будут доступны общий винрейт и лучшая пара."
        )
    lines = [
        "🤝 <b>Совместные игры</b>",
        "",
        f"🎮 Сыграно вместе: {_b(plural_games(games))} · {_fmt_wr(games, summary.get('wins', 0))}",
    ]
    duo = result.get("duo")
    if duo:
        n1, n2 = duo["pair"]
        lines.append(
            f"💞 Лучшая пара: <b>{_esc(n1)} + {_esc(n2)}</b> — "
            f"{plural_games(duo['games'])}, {_fmt_wr(duo['games'], duo['wins'])}"
        )
    return "\n".join(lines)


# --- герои --------------------------------------------------------------

def render_heroes(summaries: list[PlayerSummary]) -> str:
    if not summaries:
        return "Список игроков пуст. Для добавления используйте: /add «ссылка или ID» Имя [MMR]"
    lines = ["🦸 <b>Наиболее часто используемые герои</b>"]
    for s in summaries:
        lines.append(f"• {_b(s.display_name)}: {_heroes_line(s.top_heroes)}")
    return "\n".join(lines)


# --- карточка игрока ----------------------------------------------------

SKILL_GROUPS = [
    ("Фарм", ["gold_per_min", "last_hits_per_min", "xp_per_min"]),
    ("Урон", ["hero_damage_per_min", "tower_damage_per_min"]),
    ("Участие в боях", ["kills_per_min", "assists_per_min"]),
    ("Поддержка", ["hero_healing_per_min"]),
]


def _bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width)))
    return "▰" * filled + "▱" * (width - filled)


def _skill_block(skill: dict) -> Optional[str]:
    """Объективный профиль скилла: перцентиль по категориям (50% = средний игрок)."""
    if not skill:
        return None
    lines = ["🧠 <b>Профиль навыков</b> <i>(мировой перцентиль; 50% — средний уровень)</i>"]
    for label, metrics in SKILL_GROUPS:
        pcts = [skill[m] for m in metrics if m in skill]
        if not pcts:
            continue
        avg = sum(pcts) / len(pcts)
        lines.append(f"   {label}  {_bar(avg)} {avg * 100:.0f}%")
    return "\n".join(lines) if len(lines) > 1 else None


def _k(value) -> str:
    """Компактно: 13656 → 13.7k, 428 → 428."""
    if value is None:
        return "—"
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


def _form_icons(form: list) -> str:
    return " ".join("В" if won else "П" for won in form)


def _wr_ratio(pair: tuple) -> Optional[float]:
    games, wins = pair
    return wins / games if games else None


LEAD_NAMES = {"perf": "перф", "winrate": "винрейт", "kda": "KDA", "gpm": "GPM"}


def standing_line(comparison: dict, name: str) -> Optional[str]:
    """Строка «место в чате» для игрока (None, если сравнивать не с кем)."""
    if comparison["size"] < 2:
        return None
    player = comparison["players"].get(name)
    if not player:
        return None
    ranks = player["ranks"]
    parts = [_b(f"сила #{player['power_rank']}")]
    for key, label in (("perf", "перф"), ("winrate", "WR"), ("kda", "KDA")):
        if key in ranks:
            parts.append(f"{label} #{ranks[key]}")
    line = f"📍 Позиция в чате (из {comparison['size']}): " + " · ".join(parts)
    if player["leads"]:
        line += "\n👑 Лидирует по показателям: " + ", ".join(LEAD_NAMES[m] for m in player["leads"])
    return line


def render_compare_table(comparison: dict, summaries: list[PlayerSummary]) -> str:
    """Сравнительная таблица: игроки по «силе в чате» + ранги по метрикам."""
    order = sorted(summaries, key=lambda s: comparison["players"][s.display_name]["power_rank"])
    lines = [f"⚖️ <b>Сравнение игроков</b> <i>(участников: {comparison['size']})</i>", ""]
    for s in order:
        player = comparison["players"][s.display_name]
        ranks = player["ranks"]
        power = player["power"]
        power_str = f"{power * 100:.0f}" if power is not None else "—"
        lines.append(f"{_pos(player['power_rank'])} {_b(s.display_name)} — индекс {_b(power_str)}")
        parts = []
        if s.avg_perf is not None:
            parts.append(f"перф {s.avg_perf * 100:.0f} (#{ranks['perf']})")
        if s.games_total:
            parts.append(f"WR {s.winrate * 100:.0f}% (#{ranks['winrate']})")
            parts.append(f"KDA {s.kda_ratio:.1f} (#{ranks['kda']})")
        if s.avg_gpm_window is not None:
            parts.append(f"GPM {s.avg_gpm_window:.0f} (#{ranks['gpm']})")
        if parts:
            lines.append("    " + " · ".join(parts))
    return "\n".join(lines)


def render_player_card(s: PlayerSummary, standing: Optional[str] = None) -> str:
    """Карточка игрока — ТОЛЬКО окно отслеживания (последние игры), без карьерных срезов."""
    header = f"{_b(s.display_name)} · {_rank_with_emoji(s)}{_streak_str(s)}"
    if s.steam_name:
        header += f"\n🎮 Steam: {_b(s.steam_name)}"
    if s.games_total == 0:
        return header + "\n💤 Ранкед-игры отсутствуют"

    lines = [header, f"<i>🔎 Период анализа: последние {plural_games(s.games_total)}</i>", ""]

    # Заголовочная строка: MMR + честный перф рядом.
    perf = f"    перф {_b(f'{s.avg_perf * 100:.0f}/100')}" if s.avg_perf is not None else ""
    lines.append(f"{_b(_mmr_str(s.current_mmr))}{_trend(s.mmr_delta)}{perf}")
    lines.append(f"{s.wins_total}–{s.losses_total} ({s.winrate * 100:.0f}%)   последние игры: {_form_icons(s.recent_form)}")

    # Бой + экономика (всё за окно).
    lines.append(f"KDA {s.kda_ratio:.2f} ({s.avg_kills:.0f}/{s.avg_deaths:.0f}/{s.avg_assists:.0f})")
    econ = []
    if s.avg_gpm_window is not None:
        econ.append(f"{s.avg_gpm_window:.0f} gpm")
    if s.avg_net_worth_window is not None:
        econ.append(f"{_k(s.avg_net_worth_window)} нетворт")
    if s.avg_hero_damage_window is not None:
        econ.append(f"{_k(s.avg_hero_damage_window)} урон")
    if econ:
        lines.append("💰 Экономика: " + " · ".join(econ))

    # Объективный скилл (перцентиль в мире) + стиль/роль.
    if s.role_style:
        lines.append(f"🎭 Игровая роль: {s.role_style}")
    skill_block = _skill_block(s.skill)
    if skill_block:
        lines.append("")
        lines.append(skill_block)

    lines.append("")

    # Разрезы + подсказки.
    solo_wr, party_wr = _wr_ratio(s.solo), _wr_ratio(s.party)
    note = ""
    if solo_wr is not None and party_wr is not None and s.party[0] >= 2 and party_wr < solo_wr - 0.2:
        note = "   (в группе результат ниже)"
    lines.append(f"👤 Соло: {_fmt_wr(*s.solo)} · в группе: {_fmt_wr(*s.party)}{note}")

    if s.best_hour and s.worst_hour:
        bh, bwr = s.best_hour
        wh, wwr = s.worst_hour
        lines.append(f"⏰ Лучший час: {bh:02d}:00 ({bwr * 100:.0f}%) · худший: {wh:02d}:00 ({wwr * 100:.0f}%)")
    if s.avg_duration_min:
        lines.append(f"⏱️ Средняя длительность: {s.avg_duration_min:.0f} мин (максимум {s.max_duration_min:.0f})")
    if s.lobby_rank:
        lines.append(f"🎚️ Средний уровень лобби: {rank_label(s.lobby_rank)}")
    win, loss = s.wins_losses.get("win"), s.wins_losses.get("loss")
    if win and loss:
        lines.append(
            f"⚔️ KDA в победах: {win['avg_kda']:.1f} (смертей {win['avg_deaths']:.0f}) · "
            f"в поражениях: {loss['avg_kda']:.1f} (смертей {loss['avg_deaths']:.0f})"
        )
    if s.best_game:
        bg = s.best_game
        lines.append(
            f"⭐ Лучшая игра: {_esc(hero_name(bg['hero_id']))} {bg['kills']}/{bg['deaths']}/{bg['assists']}"
        )
    if s.longest_win_streak >= 2:
        lines.append(f"🔥 Наибольшая серия побед: {s.longest_win_streak}")
    pool = f"в пуле {s.hero_pool} · " if s.hero_pool else ""
    lines.append(f"🦸 Герои: {pool}{_heroes_line(s.top_heroes)}")

    if standing:
        lines.append("")
        lines.append(standing)

    lines.append("")
    lines.append(
        f'🔗 <a href="{dotabuff_player_url(s.account_id)}">Dotabuff</a> · '
        f'<a href="https://www.opendota.com/players/{s.account_id}">OpenDota</a>'
    )
    lines.append("<i>ℹ️ Перф — перцентиль относительно игроков на том же герое.</i>")
    return "\n".join(lines)


# --- список игроков -----------------------------------------------------

def render_player_list(summaries: list[PlayerSummary]) -> str:
    """Список игроков: ранг, текущий MMR (оценка) со стартом и дельтой, игры, серия, id аккаунта."""
    if not summaries:
        return "Список игроков пуст. Для добавления используйте: /add «ссылка или ID» Имя [MMR]"
    blocks = []
    for i, s in enumerate(summaries, start=1):
        if s.current_mmr is not None:
            mmr = f"🎯 {_b(_mmr_str(s.current_mmr))} (старт {s.anchor_mmr}{_trend(s.mmr_delta)})"
        else:
            mmr = "🎯 MMR не указан (задать: /setmmr)"
        if s.games_total:
            games = (
                f"🎮 {plural_games(s.games_total)} · {s.wins_total}–{s.losses_total} "
                f"({s.winrate * 100:.0f}%){_streak_str(s)}"
            )
        else:
            games = "💤 игр пока нет"
        blocks.append(
            f"{_pos(i)} {_b(s.display_name)} · {_rank_with_emoji(s)}\n"
            f"    {mmr}\n    {games}\n    🆔 id {s.account_id}"
        )
    return f"👥 <b>Отслеживаемые игроки</b> · {len(summaries)}\n\n" + "\n\n".join(blocks)


# --- Steam-профиль ------------------------------------------------------

def country_flag(code) -> str:
    """ISO-код страны → эмодзи-флаг (пусто, если код некорректный)."""
    if not isinstance(code, str) or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


def render_steam_profile(display_name: str, account_id: int, profile: dict, rank: str) -> str:
    """Подпись к аватарке: текущий ник в Steam, ник в боте, ссылка, id, ранг, страна, Dota Plus."""
    steam_name = profile.get("personaname")
    flag = country_flag(profile.get("loccountrycode"))
    lines = [f"🎮 <b>{_esc(steam_name or display_name)}</b>{(' ' + flag) if flag else ''}"]
    if steam_name and steam_name != display_name:
        lines.append(f"🏷️ В боте: {_b(display_name)}")
    lines.append(f"🏅 {_esc(rank)}")
    if profile.get("plus"):
        lines.append("💎 Dota Plus")
    if profile.get("profileurl"):
        lines.append(f"🔗 {_esc(profile['profileurl'])}")
    ids = f"🆔 account {account_id}"
    if profile.get("steamid"):
        ids += f" · SteamID64 {_esc(profile['steamid'])}"
    lines.append(ids)
    last = profile.get("last_login")
    if last:
        lines.append(f"🕒 Последний вход в Dota: {_esc(str(last)[:16].replace('T', ' '))} UTC")
    return "\n".join(lines)


def render_steam_change(player, changes: dict) -> str:
    """Оповещение о смене ника и/или аватарки Steam."""
    lines = []
    if "name" in changes:
        old, new = changes["name"]
        lines.append(f"🔔 {_b(player.display_name)} сменил ник в Steam: {_esc(old)} ➜ {_b(new)}")
    if changes.get("avatar"):
        lines.append(f"🖼️ {_b(player.display_name)} сменил аватарку в Steam")
    return "\n".join(lines)


# --- оповещения, достижения, недельная сводка --------------------------

def render_game_alert(event: dict) -> str:
    """Оповещение о новой игре: по строке на каждого отслеживаемого игрока этого матча."""
    duration = f" · {event['duration'] // 60} мин" if event.get("duration") else ""
    lines = [f'🎮 <b>Новая игра</b>{duration} · <a href="{dotabuff_match_url(event["match_id"])}">Dotabuff</a>']
    for r in event["rows"]:
        delta = r["step"] if r["won"] else -r["step"]
        line = (
            f"{'🏆' if r['won'] else '💀'} {_b(r['name'])} — {_esc(hero_name(r['hero_id']))} "
            f"{r['kills']}/{r['deaths']}/{r['assists']} · {_today_delta(delta)}"
        )
        if r.get("current_mmr") is not None:
            line += f" ➜ ≈{r['current_mmr']} MMR"
        if r.get("streak_len", 0) >= 3:
            line += f"  {'🔥' if r['streak_type'] == 'W' else '💧'}{r['streak_len']} подряд"
        lines.append(line)
    shared = event.get("shared")
    if shared and shared.get("games"):
        lines.append(f"🤝 Сегодня вместе: {plural_games(shared['games'])} · {shared['wins']}–{shared['losses']}")
    return "\n".join(lines)


def _achievement_text(code: str, detail) -> str:
    from mmrbot.achievements import CATALOG
    ach = CATALOG.get(code)
    if ach is None:
        return _esc(code)
    extra = f" <i>({_esc(detail)})</i>" if detail and not str(detail).isdigit() else ""
    return f"{ach.emoji} <b>{_esc(ach.title)}</b>{extra}"


def render_achievement_alert(event: dict) -> str:
    from mmrbot.achievements import CATALOG
    lines = []
    for code, detail in event["items"]:
        ach = CATALOG.get(code)
        if ach is not None and ach.anti:
            lines.append(f"🤡 {_b(event['name'])} заработал антирекорд: {_achievement_text(code, detail)}")
        else:
            lines.append(f"🏅 {_b(event['name'])} получил достижение: {_achievement_text(code, detail)}")
    return "\n".join(lines)


def render_achievements(rows: list) -> str:
    """rows: [(имя, {code: (earned_ts, detail)})] — достижения и антирекорды по игрокам."""
    from datetime import datetime, timezone
    from mmrbot.achievements import CATALOG
    if not rows:
        return "Список игроков пуст. Для добавления используйте: /add «ссылка или ID» Имя [MMR]"
    blocks = []
    for name, earned in rows:
        items = [(c, v) for c, v in earned.items() if c in CATALOG]
        head = f"{_b(name)} · {len(items)}"
        if not items:
            blocks.append(f"{head}\n    💤 пока нет достижений")
            continue
        items.sort(key=lambda kv: (CATALOG[kv[0]].anti, kv[1][0]))
        body = []
        for code, (ts, detail) in items:
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")
            body.append(f"    {_achievement_text(code, detail)} · {date}")
        blocks.append(head + "\n" + "\n".join(body))
    return "🏅 <b>Достижения и антирекорды</b>\n\n" + "\n\n".join(blocks)


def render_weekly(report: dict) -> str:
    rows = [r for r in report["rows"] if r["games"] > 0]
    if not rows:
        return "📅 <b>Итоги недели</b>\n\n💤 За неделю ранкед-игр не было."
    games = sum(r["games"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    lines = ["📅 <b>Итоги недели</b> · ранкед", "", f"🎮 Всего: {plural_games(games)} · {_fmt_wr(games, wins)}"]
    best = max(rows, key=lambda r: r["delta"])
    worst = min(rows, key=lambda r: r["delta"])
    if best["delta"] > 0:
        lines.append(f"🚀 Больше всех поднялся: {_b(best['name'])} {_today_delta(best['delta'])} ({best['wins']}–{best['losses']})")
    if worst["delta"] < 0 and worst is not best:
        lines.append(f"📉 Больше всех просел: {_b(worst['name'])} {_today_delta(worst['delta'])} ({worst['wins']}–{worst['losses']})")
    busiest = max(rows, key=lambda r: r["games"])
    lines.append(f"🕹️ Больше всех играл: {_b(busiest['name'])} — {plural_games(busiest['games'])}")
    hero = report.get("hero")
    if hero:
        lines.append(
            f"🦸 Герой недели: {_b(hero_name(hero['hero_id']))} — {plural_games(hero['games'])} · "
            f"{hero['wins'] / hero['games'] * 100:.0f}%"
        )
    if report.get("streak"):
        name, length = report["streak"]
        lines.append(f"🔥 Лучшая серия: {_b(name)} — {length} побед подряд")
    shared = report.get("shared") or {}
    if shared.get("games"):
        lines.append(f"🤝 Вместе: {plural_games(shared['games'])} · {shared['wins']}–{shared['losses']}")
    lines.append("")
    for i, r in enumerate(sorted(rows, key=lambda r: r["delta"], reverse=True), start=1):
        lines.append(
            f"{_pos(i)} {_b(r['name'])} · {_today_delta(r['delta'])} · "
            f"{r['wins']}–{r['losses']} ({r['winrate'] * 100:.0f}%)"
        )
    return "\n".join(lines)


def render_records(data: dict, period: str) -> str:
    """Рекорды пати за период: лучшая отдельная игра по каждому показателю (герой, значение, матч)."""
    from datetime import datetime, timezone
    title = f"🌟 <b>Рекорды пати {PERIOD_LABELS.get(period, '')}</b>"
    records = data.get("records") or []
    if not records:
        return title + "\n\n💤 За выбранный период нет данных (нужны сыгранные игры; GPM, урон и нетворт подгружаются фоном)."
    lines = [title, ""]
    for r in records:
        match = r["match"]
        date = datetime.fromtimestamp(match["start_time"], tz=timezone.utc).strftime("%d.%m.%y")
        lines.append(
            f"{r['emoji']} {r['title']}: {_b(r['player'])} — <b>{_esc(r['text'])}</b>\n"
            f"      {_esc(hero_name(match.get('hero_id')))} · {date} · "
            f'<a href="{dotabuff_match_url(match["match_id"])}">матч {match["match_id"]}</a>'
        )
    if data.get("streak"):
        name, length = data["streak"]
        lines.append(f"🔥 Лучшая серия побед: {_b(name)} — {length} подряд")
    return "\n".join(lines)


# --- настройки чата -----------------------------------------------------

def tz_label(tz_name: str) -> str:
    from mmrbot.keyboards import TIMEZONES
    for label, name in TIMEZONES:
        if name == tz_name:
            return f"{label} ({name})"
    return tz_name


def render_settings(chat) -> str:
    def state(flag: bool, on: str = "включены", off: str = "выключены") -> str:
        return on if flag else off

    return (
        "⚙️ <b>Настройки чата</b>\n\n"
        f"🎯 Шаг оценки MMR: <b>±{chat.mmr_step}</b> за ранкед-игру\n"
        f"⏰ Ежедневная сводка: <b>{chat.digest_hour:02d}:00</b>\n"
        f"🌍 Часовой пояс: <b>{_esc(tz_label(chat.tz))}</b>\n"
        f"🔔 Оповещения о смене ника/аватарки Steam: <b>{state(chat.notify_steam)}</b>\n"
        f"🎮 Оповещения о новых играх и достижениях: <b>{state(chat.notify_games)}</b>\n"
        f"📅 Недельная сводка (понедельник): <b>{state(chat.notify_weekly, 'включена', 'выключена')}</b>\n\n"
        "<i>Часовой пояс влияет на «сегодня» и время сводки.</i>"
    )


# --- герои игрока, роли, матч, герой ------------------------------------

PERIOD_LABELS = {"day": "за сутки", "week": "за неделю", "month": "за месяц", "year": "за год", "all": "за всё время"}
POSITION_NAMES = {1: "Pos 1 · Керри", 2: "Pos 2 · Мид", 3: "Pos 3 · Оффлейн",
                  4: "Pos 4 · Роумер", 5: "Pos 5 · Фулл-саппорт"}
POSITION_EMOJI = {1: "🗡️", 2: "🎯", 3: "🛡️", 4: "🧭", 5: "💚"}
LANE_LABELS = {"SAFE_LANE": "Safe", "MID_LANE": "Mid", "OFF_LANE": "Off", "JUNGLE": "Лес"}


def _imp(value) -> str:
    if value is None:
        return "—"
    rounded = round(value)
    return f"{rounded:+d}" if rounded else "0"


def _wr_dot(winrate: float) -> str:
    """Цветная метка винрейта: 🟢 ≥55%, 🟡 48–54%, 🔴 ниже."""
    pct = winrate * 100
    return "🟢" if pct >= 55 else "🟡" if pct >= 48 else "🔴"


def _stat_line(s: dict) -> str:
    parts = [f"{s['games']}и", f"{s['winrate'] * 100:.0f}%", f"KDA {s['kda']:.1f}"]
    if s.get("avg_imp") is not None:
        parts.append(f"IMP {_imp(s['avg_imp'])}")
    if s.get("avg_gpm") is not None:
        parts.append(f"GPM {s['avg_gpm']:.0f}")
    return " · ".join(parts)


def _stat_detail(s: dict) -> str:
    """Вторая строка блока: 🟢 винрейт · ⚔️ KDA · 📊 IMP · 💰 GPM (с отступом под эмодзи места)."""
    parts = [f"{_wr_dot(s['winrate'])} {s['winrate'] * 100:.0f}%", f"⚔️ KDA {s['kda']:.1f}"]
    if s.get("avg_imp") is not None:
        parts.append(f"📊 IMP {_imp(s['avg_imp'])}")
    if s.get("avg_gpm") is not None:
        parts.append(f"💰 GPM {s['avg_gpm']:.0f}")
    return "      " + " · ".join(parts)


def _stat_block(head: str, s: dict) -> str:
    return f"{head} · {s['games']}и\n{_stat_detail(s)}"


def render_player_heroes(name: str, period: str, rows: list[dict], limit: int = 10) -> str:
    title = f"🦸 <b>Герои: {_esc(name)}</b> · <i>{PERIOD_LABELS.get(period, '')}</i>"
    if not rows:
        return title + "\n😴 За указанный период игр нет."
    blocks = [
        _stat_block(f"{_pos(i)} {_b(hero_name(s['hero_id']))}", s)
        for i, s in enumerate(rows[:limit], 1)
    ]
    text = title + "\n\n" + "\n".join(blocks)
    if len(rows) > limit:
        rest = len(rows) - limit
        text += f"\n\n➕ и ещё {rest} {plural_heroes(rest)}"
    return text


def render_roles(name: str, rows: list[dict], period: str = "all") -> str:
    title = f"🧭 <b>Позиции: {_esc(name)}</b> · <i>{PERIOD_LABELS.get(period, '')}</i>"
    if not rows:
        return title + "\n😴 Данные о позициях отсутствуют (требуются STRATZ_API_KEY и сыгранные матчи)."
    blocks = []
    for s in rows:
        emoji = POSITION_EMOJI.get(s["position"], "▫️")
        label = POSITION_NAMES.get(s["position"], "Pos " + str(s["position"]))
        blocks.append(_stat_block(f"{emoji} {_b(label)}", s))
    return title + "\n\n" + "\n".join(blocks)


def render_match_card(view: dict) -> str:
    from datetime import datetime, timezone
    from mmrbot.stats import is_win
    player, row = view["player"], view["match"]
    win = is_win(row["player_slot"], row["radiant_win"])
    when = datetime.fromtimestamp(row["start_time"], tz=timezone.utc).strftime("%d.%m %H:%M UTC")
    duration = f" · {row['duration'] // 60} мин" if row.get("duration") else ""
    lines = [
        f"🎮 <b>Матч {row['match_id']}</b> <i>{when}{duration}</i>",
        f"{'🏆 Победа' if win else '💀 Поражение'} — {_b(player.display_name)} на {_b(hero_name(row.get('hero_id')))}",
        f"⚔️ K/D/A: {row.get('kills', 0)}/{row.get('deaths', 0)}/{row.get('assists', 0)}",
    ]
    if row.get("position"):
        role = POSITION_NAMES.get(row["position"], f"Pos {row['position']}")
        lane = LANE_LABELS.get(row.get("lane") or "")
        lines.append(f"🧭 Позиция: {role}" + (f" · {lane}" if lane else ""))
    if row.get("imp") is not None:
        lines.append(f"📊 IMP {_imp(row['imp'])}")
    farm = []
    if row.get("gpm") is not None:
        farm.append(f"GPM {row['gpm']:.0f}")
    if row.get("xpm") is not None:
        farm.append(f"XPM {row['xpm']:.0f}")
    if row.get("last_hits") is not None:
        farm.append(f"LH {row['last_hits']}/{row.get('denies') or 0}")
    if row.get("net_worth") is not None:
        farm.append(f"NW {_k(row['net_worth'])}")
    if farm:
        lines.append("💰 Экономика: " + " · ".join(farm))
    dmg = []
    if row.get("hero_damage") is not None:
        dmg.append(f"урон {_k(row['hero_damage'])}")
    if row.get("tower_damage") is not None:
        dmg.append(f"башни {_k(row['tower_damage'])}")
    if row.get("hero_healing"):
        dmg.append(f"хил {_k(row['hero_healing'])}")
    if dmg:
        lines.append("💥 Влияние: " + " · ".join(dmg))
    lines.append(f'🔗 <a href="{dotabuff_match_url(row["match_id"])}">Матч на Dotabuff</a>')
    return "\n".join(lines)


def render_hero_detail(hero_id: int, period: str, entries: list) -> str:
    title = f"🦸 <b>Герой: {_esc(hero_name(hero_id))}</b> · <i>{PERIOD_LABELS.get(period, '')}</i>"
    if not entries:
        return title + "\n😴 На данном герое участники пати не играли."
    blocks = [_stat_block(f"{_pos(i)} {_b(player.display_name)}", s) for i, (player, s) in enumerate(entries, 1)]
    return title + "\n\n" + "\n".join(blocks)


def _team_lines(players: list[dict], tracked: dict) -> list[str]:
    lines = []
    for p in sorted(players, key=lambda x: (x.get("position") or 9)):
        mark = "★ " if p.get("account_id") in tracked else ""
        name = tracked.get(p.get("account_id")) or p.get("name") or "—"
        pos = f"P{p['position']}" if p.get("position") else "—"
        lines.append(
            f"{mark}{_b(name)} · {_esc(hero_name(p.get('hero_id')))} "
            f"{p.get('kills') or 0}/{p.get('deaths') or 0}/{p.get('assists') or 0} · {pos} · IMP {_imp(p.get('imp'))}"
        )
    return lines


def render_full_match(match: dict, tracked: dict, focus=None) -> str:
    """Полный матч (все 10 игроков). tracked: {account_id: имя в боте} — отмечаются ★.

    focus — account_id игрока, чья подробная карточка (GPM, урон…) идёт сверху; без него — только шапка.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace
    players = match.get("players") or []
    radiant = [p for p in players if p["is_radiant"]]
    dire = [p for p in players if not p["is_radiant"]]
    target = next((p for p in players if p.get("account_id") == focus), None)
    if target is not None:
        row = dict(target, match_id=match["match_id"], start_time=match.get("start_time") or 0,
                   duration=match.get("duration"), radiant_win=match.get("radiant_win"),
                   player_slot=0 if target["is_radiant"] else 128)
        name = tracked.get(focus) or target.get("name") or "—"
        head = render_match_card({"player": SimpleNamespace(display_name=name), "match": row})
    else:
        when = datetime.fromtimestamp(match.get("start_time") or 0, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
        duration = f" · {match['duration'] // 60} мин" if match.get("duration") else ""
        head = (
            f"🎮 <b>Матч {match['match_id']}</b> <i>{when}{duration}</i>\n"
            f'🔗 <a href="{dotabuff_match_url(match["match_id"])}">Матч на Dotabuff</a>'
        )
    winner = "Radiant" if match.get("radiant_win") else "Dire"
    lines = [head, "", f"🏆 Победитель: {winner}", "🌕 <b>Radiant</b>"]
    lines += _team_lines(radiant, tracked)
    lines += ["🌑 <b>Dire</b>"] + _team_lines(dire, tracked)
    return "\n".join(lines)
