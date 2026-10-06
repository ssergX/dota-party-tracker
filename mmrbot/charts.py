"""Графики (PNG в памяти). matplotlib подключается лениво — тесты и старт бота остаются лёгкими."""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytz

PALETTE = ["#4cc9f0", "#f72585", "#ffb703", "#b5e48c", "#9d4edd", "#ff7b00", "#2ec4b6", "#e5e5e5"]
BG, PANEL, FG, MUTED, GRID = "#0b1118", "#16202b", "#e8eef5", "#8493a3", "#243140"
CARD_EDGE = "#202c3a"  # тёмная тема Telegram
WIN, LOSS = "#3ddc84", "#ff5c5c"
MAX_POINTS = 300  # длиннее — прореживаем (история в тысячи игр рисовалась бы долго и выглядела кашей)
DOTS_LIMIT = 80  # отдельные точки-игры рисуем только на коротких сериях


def _games_word(n: int) -> str:
    n10, n100 = n % 10, n % 100
    word = "игра" if n10 == 1 and n100 != 11 else "игры" if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else "игр"
    return f"{n} {word}"


def series_stats(points: list[tuple[int, int]]) -> tuple[int, int, int]:
    """(игр, побед, итог ±MMR) по серии накопленных значений."""
    wins, prev = 0, 0
    for _, value in points:
        wins += 1 if value > prev else 0
        prev = value
    return len(points), wins, points[-1][1]


def _spread_labels(ys: list[float], min_gap: float) -> list[float]:
    """Раздвигает подписи итогов, чтобы они не наезжали друг на друга (порядок по y сохраняется)."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    placed = list(ys)
    for prev, cur in zip(order, order[1:]):
        if placed[cur] - placed[prev] < min_gap:
            placed[cur] = placed[prev] + min_gap
    shift = (sum(placed) - sum(ys)) / len(ys)  # возвращаем облако подписей к центру исходных значений
    return [y - shift for y in placed]


def _thin(points: list[tuple[int, int]], limit: int = MAX_POINTS) -> list[tuple[int, int]]:
    """Равномерно прореживает серию, сохраняя первую и последнюю точки."""
    if len(points) <= limit:
        return points
    buckets = limit // 3
    stride = len(points) / buckets
    picked: list[tuple[int, int]] = []
    for i in range(buckets):  # в каждой корзине оставляем минимум, максимум и последнюю точку — пики не теряются
        chunk = points[int(i * stride):int((i + 1) * stride)] or [points[-1]]
        keep = {min(chunk, key=lambda p: p[1]), max(chunk, key=lambda p: p[1]), chunk[-1]}
        if i == 0:
            keep.add(chunk[0])  # начало серии сохраняем
        picked.extend(sorted(keep, key=lambda p: p[0]))
    if picked[-1] != points[-1]:
        picked.append(points[-1])
    return picked


def warmup() -> None:
    """Прогрев matplotlib (импорт и кэш шрифтов) — вызывается фоном при старте, чтобы первый график был быстрым."""
    try:
        render_mmr_chart({"w": [(1, 25), (2, 0)]}, "w", "UTC")
    except Exception:
        pass


def render_mmr_chart(
    series: dict[str, list[tuple[int, int]]], title: str, tz_name: str,
    since_ts: int | None = None, until_ts: int | None = None, by_games: bool = False,
) -> bytes:
    """Динамика ±MMR по игрокам: {имя: [(unix-время, накопленное Δ)]} → PNG-байты.

    Карточка: шапка (заголовок, период, лидер) → график (линия со свечением, значения-«таблетки» справа,
    у одного игрока — градиентная заливка, зелёные/красные точки-игры, пик и просадка) → таблица игроков
    (итог, игры, полоска винрейта).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.patheffects as pe
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import FancyBboxPatch, Rectangle
    from matplotlib.path import Path
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")

    def to_dt(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)

    def to_x(ts: float) -> float:
        return ts if by_games else mdates.date2num(to_dt(ts))

    if by_games:  # ось X — порядковый номер игры: серии разных игроков сравнимы «игра к игре»
        series = {name: [(i, v) for i, (_, v) in enumerate(pts, 1)] for name, pts in series.items()}
        since_ts = until_ts = None

    ordered = sorted(series.items(), key=lambda kv: kv[1][-1][1], reverse=True)
    n = len(ordered)
    solo = n == 1

    # --- раскладка в дюймах (1 единица оверлея = 1 дюйм) ---
    W = 11.0
    row_h, table_head = 0.38, 0.5
    table_h = table_head + row_h * n + 0.12
    plot_h = 3.9
    footer_h = 0.42
    head_h = 1.15
    H = footer_h + table_h + 0.25 + plot_h + 0.12 + head_h
    table_y0 = footer_h
    card_y0 = table_y0 + table_h + 0.25
    card_y1 = card_y0 + plot_h
    ax_box = (1.0, card_y0 + 0.5, W - 0.45 - 0.95, card_y1 - 0.3)  # x0, y0, x1, y1

    plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans"]
    fig = plt.figure(figsize=(W, H), dpi=120)
    fig.patch.set_facecolor(BG)
    back = fig.add_axes([0, 0, 1, 1], zorder=0)
    back.set_xlim(0, W)
    back.set_ylim(0, H)
    back.axis("off")
    gradient = np.linspace(0, 1, 256).reshape(-1, 1)
    back.imshow(gradient, extent=(0, W, 0, H), aspect="auto", cmap=_bg_cmap(), zorder=-10, origin="upper")

    def card(x0, y0, x1, y1):
        back.add_patch(FancyBboxPatch(
            (x0, y0), x1 - x0, y1 - y0, boxstyle="round,pad=0,rounding_size=0.16", linewidth=1,
            edgecolor=CARD_EDGE, facecolor=PANEL, zorder=1,
        ))

    card(0.4, card_y0, W - 0.4, card_y1)
    card(0.4, table_y0, W - 0.4, table_y0 + table_h)

    ax = fig.add_axes(
        [ax_box[0] / W, ax_box[1] / H, (ax_box[2] - ax_box[0]) / W, (ax_box[3] - ax_box[1]) / H], zorder=2
    )
    ax.set_facecolor("none")

    all_values = [v for _, pts in ordered for _, v in pts] + [0]
    top, low = max(all_values), min(all_values)
    pad = max(25, (top - low) * 0.16)
    y_lo, y_hi = low - pad, top + pad
    ax.set_ylim(y_lo, y_hi)
    ax.axhspan(0, y_hi, color=WIN, alpha=0.045, zorder=0, linewidth=0)
    ax.axhspan(y_lo, 0, color=LOSS, alpha=0.045, zorder=0, linewidth=0)
    ax.axhline(0, color=FG, linewidth=1, alpha=0.35, linestyle=(0, (4, 4)), zorder=1)

    ends: list[tuple] = []
    x_min = x_max = None
    for i, (name, points) in enumerate(ordered):
        color = PALETTE[i % len(PALETTE)]
        games, wins, total = series_stats(points)
        shown = _thin(points)
        start_ts = 0 if by_games else since_ts if since_ts is not None and since_ts < shown[0][0] else shown[0][0]
        xs = [to_x(start_ts)] + [to_x(ts) for ts, _ in shown]  # линия стартует с нуля в начале периода
        ys = [0] + [value for _, value in shown]
        x_min = xs[0] if x_min is None else min(x_min, xs[0])
        x_max = xs[-1] if x_max is None else max(x_max, xs[-1])
        line, = ax.plot(xs, ys, color=color, linewidth=2.6 if solo else 2.3, zorder=3,
                        solid_joinstyle="round", solid_capstyle="round")
        line.set_path_effects([pe.Stroke(linewidth=7, foreground=color, alpha=0.16), pe.Normal()])
        if solo:  # градиентная заливка: плотнее у линии, к нулю растворяется
            poly = ax.fill_between(xs, ys, 0, alpha=0, linewidth=0)
            span = max(abs(y_lo), abs(y_hi))
            col = np.linspace(y_hi, y_lo, 256).reshape(-1, 1)
            rgba = np.zeros((256, 1, 4))
            rgba[..., :3] = matplotlib.colors.to_rgb(color)
            rgba[..., 3] = np.clip(np.abs(col) / span, 0, 1) * 0.42
            image = ax.imshow(rgba, extent=(xs[0], xs[-1], y_lo, y_hi), aspect="auto", zorder=2, origin="upper")
            image.set_clip_path(Path.make_compound_path(*poly.get_paths()), ax.transData)
        if len(shown) <= DOTS_LIMIT:
            dots = [(x, y, y > before) for x, y, before in zip(xs[1:], ys[1:], ys[:-1])]
            if solo:  # цвет точки — исход игры
                ax.scatter([d[0] for d in dots], [d[1] for d in dots], s=44, zorder=5, edgecolors=PANEL,
                           linewidths=1.6, c=[WIN if d[2] else LOSS for d in dots])
            else:  # цвет точки — игрок (исход виден по направлению линии)
                ax.scatter([d[0] for d in dots], [d[1] for d in dots], s=22, zorder=5, edgecolors=PANEL,
                           linewidths=0.9, c=color)
        ends.append((xs[-1], ys[-1], total, color))
        if solo and games >= 3:  # у одного игрока подписываем пик и просадку
            full_ys = [value for _, value in points]
            hi, lo = max(full_ys), min(full_ys)
            hi_ts, lo_ts = points[full_ys.index(hi)][0], points[full_ys.index(lo)][0]
            tag = {"fontsize": 9, "fontweight": "bold", "ha": "center", "zorder": 7, "textcoords": "offset points"}
            if hi > 0:
                ax.annotate(f"▲ пик {hi:+d}", (to_x(hi_ts), hi), xytext=(0, 12), color=WIN, **tag)
            if lo < 0:
                ax.annotate(f"▼ просадка {lo:+d}", (to_x(lo_ts), lo), xytext=(0, -19), color=LOSS, **tag)

    # --- ось X ---
    if by_games:
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=10))
        ax.set_xlabel("номер игры", color=MUTED, fontsize=9.5, labelpad=8)
        span = max(x_max - x_min, 1)
        ax.set_xlim(x_min - span * 0.02, x_max + span * 0.04)
    else:
        ax.xaxis_date(tz=tz)
        locator = mdates.AutoDateLocator(tz=tz, maxticks=9)
        ax.xaxis.set_major_locator(locator)
        fmt = mdates.ConciseDateFormatter(locator, tz=tz)
        fmt.formats = ["%Y", "%m.%Y", "%d.%m", "%H:%M", "%H:%M", "%H:%M"]
        fmt.zero_formats = ["", "%Y", "%d.%m", "%d.%m", "%H:%M", "%H:%M"]
        fmt.offset_formats = [""] * 6  # без подписи «2026-Oct» справа внизу
        ax.xaxis.set_major_formatter(fmt)
        if since_ts is not None and until_ts is not None:  # ось — выбранный период целиком
            ax.set_xlim(to_x(since_ts), to_x(until_ts + (until_ts - since_ts) // 30))
        else:
            span = max(x_max - x_min, 1e-3)
            ax.set_xlim(x_min - span * 0.02, x_max + span * 0.06)
        if until_ts is not None:  # после последней игры значение держится до «сейчас»
            for x_end, y_end, _, color in ends:
                if to_x(until_ts) > x_end:
                    ax.hlines(y_end, x_end, to_x(until_ts), color=color, linewidth=1.3, linestyles=":",
                              alpha=0.6, zorder=3)
    ax.set_ylim(y_lo, y_hi)

    # --- значения-«таблетки» справа от линий ---
    label_ys = _spread_labels([e[1] for e in ends], (y_hi - y_lo) * 0.075)
    for (_, _, total, color), y_label in zip(ends, label_ys):
        ax.annotate(
            f"{total:+d}" if total else "0", (1.0, y_label), xycoords=("axes fraction", "data"), xytext=(8, 0),
            textcoords="offset points", va="center", ha="left", color=BG, fontsize=10.5, fontweight="bold",
            zorder=8, annotation_clip=False,
            bbox={"boxstyle": "round,pad=0.32,rounding_size=0.5", "facecolor": color, "edgecolor": "none"},
        )

    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=7))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}" if v else "0"))
    ax.tick_params(colors=MUTED, labelsize=9, length=0, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.7, alpha=0.9)
    ax.grid(axis="x", color=GRID, linewidth=0.5, alpha=0.45)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)

    # --- шапка ---
    back.text(0.55, H - 0.5, title, color=FG, fontsize=19, fontweight="bold", ha="left", va="center")
    if since_ts is not None and until_ts is not None:
        sub = f"{to_dt(since_ts):%d.%m.%Y} — {to_dt(until_ts):%d.%m.%Y}"
    else:
        sub = "по порядку игр" if by_games else "все игры"
    back.text(0.55, H - 0.88, sub, color=MUTED, fontsize=10.5, ha="left", va="center")
    lead_name, lead_pts = ordered[0]
    lead_total = series_stats(lead_pts)[2]
    back.text(W - 0.55, H - 0.4, "ИТОГ" if solo else "ЛИДЕР", color=MUTED, fontsize=8.5, fontweight="bold",
              ha="right", va="center")
    back.text(W - 0.55, H - 0.78, f"{lead_name}  {lead_total:+d}" if lead_total else f"{lead_name}  0",
              color=PALETTE[0], fontsize=15, fontweight="bold", ha="right", va="center")

    # --- таблица игроков ---
    cols = {"name": 0.75, "total": 5.0, "games": 6.55, "wr": 7.75, "bar": (8.0, 10.45)}
    head_y = table_y0 + table_h - 0.3
    for key, text, ha in (("name", "ИГРОК", "left"), ("total", "±MMR", "right"), ("games", "ИГРЫ", "right"),
                          ("wr", "ПОБЕДЫ", "right")):
        back.text(cols[key], head_y, text, color=MUTED, fontsize=8, fontweight="bold", ha=ha, va="center")
    for i, (name, points) in enumerate(ordered):
        color = PALETTE[i % len(PALETTE)]
        games, wins, total = series_stats(points)
        y = table_y0 + table_h - table_head - row_h * i - row_h / 2 + 0.06
        if i:
            back.plot([0.65, W - 0.65], [y + row_h / 2, y + row_h / 2], color=GRID, linewidth=0.7, zorder=2)
        back.add_patch(FancyBboxPatch((0.62, y - 0.07), 0.14, 0.14, boxstyle="round,pad=0,rounding_size=0.07",
                                      facecolor=color, edgecolor="none", zorder=3))
        shown_name = name if len(name) <= 22 else name[:21] + "…"
        back.text(0.9, y, shown_name, color=FG, fontsize=11.5, fontweight="bold", ha="left", va="center", zorder=3)
        back.text(cols["total"], y, f"{total:+d}" if total else "0",
                  color=WIN if total > 0 else LOSS if total < 0 else MUTED,
                  fontsize=12, fontweight="bold", ha="right", va="center", zorder=3)
        back.text(cols["games"], y, _games_word(games), color=FG, fontsize=10.5, ha="right", va="center", zorder=3)
        rate = wins / games
        back.text(cols["wr"], y, f"{rate * 100:.0f}%", color=FG, fontsize=10.5, ha="right", va="center", zorder=3)
        bx0, bx1 = cols["bar"]
        back.add_patch(FancyBboxPatch((bx0, y - 0.05), bx1 - bx0, 0.1, boxstyle="round,pad=0,rounding_size=0.05",
                                      facecolor=GRID, edgecolor="none", zorder=3))
        if rate > 0:
            back.add_patch(FancyBboxPatch((bx0, y - 0.05), max(0.1, (bx1 - bx0) * rate), 0.1,
                                          boxstyle="round,pad=0,rounding_size=0.05", facecolor=color,
                                          edgecolor="none", zorder=4))
        back.add_patch(Rectangle((bx0 + (bx1 - bx0) / 2 - 0.01, y - 0.08), 0.02, 0.16, facecolor=FG, alpha=0.5,
                                 edgecolor="none", zorder=5))  # отметка 50%

    note = "оценка: стартовое значение ± шаг за каждую ранкед-игру"
    if solo:
        note = "точки: зелёная — победа, красная — поражение  ·  " + note
    back.text(W / 2, footer_h / 2, note, color=MUTED, fontsize=8.5, ha="center", va="center")

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()


def _bg_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("bg", ["#131d28", "#0b1118"])
