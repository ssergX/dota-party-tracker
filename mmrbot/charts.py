"""Графики (PNG в памяти). matplotlib подключается лениво — тесты и старт бота остаются лёгкими."""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytz

PALETTE = ["#4cc9f0", "#f72585", "#ffb703", "#b5e48c", "#9d4edd", "#ff7b00", "#2ec4b6", "#e5e5e5"]
BG, PANEL, FG, MUTED, GRID = "#0f1720", "#17212b", "#e6edf3", "#8b9bab", "#263340"  # тёмная тема Telegram
WIN, LOSS = "#3ddc84", "#ff5c5c"
MAX_POINTS = 300  # длиннее — прореживаем (история в тысячи игр рисовалась бы долго и выглядела кашей)
DOTS_LIMIT = 80  # отдельные точки-игры рисуем только на коротких сериях


def _games_word(n: int) -> str:
    n10, n100 = n % 10, n % 100
    word = "игра" if n10 == 1 and n100 != 11 else "игры" if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else "игр"
    return f"{n} {word}"


def _stats(points: list[tuple[int, int]]) -> tuple[int, int, int]:
    """(игр, побед, итог ±MMR) по серии накопленных значений."""
    wins, prev = 0, 0
    for _, value in points:
        wins += 1 if value > prev else 0
        prev = value
    return len(points), wins, points[-1][1]


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
    since_ts: int | None = None, until_ts: int | None = None,
) -> bytes:
    """Динамика ±MMR по игрокам: {имя: [(unix-время, накопленное Δ)]} → PNG-байты.

    Линия — накопленный итог (с мягкой заливкой), точки — отдельные игры (зелёная — победа, красная —
    поражение; только на коротких сериях), ▲/▼ — пик и просадка, в легенде: итог, игры и винрейт.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")

    def to_dt(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)

    ordered = sorted(series.items(), key=lambda kv: kv[1][-1][1], reverse=True)
    fig = plt.figure(figsize=(10, 5.6 + 0.3 * len(ordered)), dpi=110)
    fig.patch.set_facecolor(BG)
    legend_h = 0.07 + 0.045 * len(ordered)
    ax = fig.add_axes([0.075, legend_h + 0.08, 0.895, 0.80 - legend_h])
    ax.set_facecolor(PANEL)

    all_values = [v for _, pts in ordered for _, v in pts] + [0]
    top, low = max(all_values), min(all_values)
    pad = max(25, (top - low) * 0.14)
    ax.set_ylim(low - pad, top + pad)
    ax.axhspan(0, top + pad, color=WIN, alpha=0.05, zorder=0)
    ax.axhspan(low - pad, 0, color=LOSS, alpha=0.05, zorder=0)
    ax.axhline(0, color=FG, linewidth=1, alpha=0.4, zorder=1)

    for i, (name, points) in enumerate(ordered):
        color = PALETTE[i % len(PALETTE)]
        games, wins, total = _stats(points)
        shown = _thin(points)
        short = len(shown) <= DOTS_LIMIT
        xs = [to_dt(shown[0][0])] + [to_dt(ts) for ts, _ in shown]
        ys = [0] + [value for _, value in shown]
        label = f"{name}   {total:+d} MMR · {_games_word(games)} · {wins / games * 100:.0f}% побед"
        if short:
            ax.step(xs, ys, where="post", color=color, linewidth=2.6, label=label, zorder=3, solid_capstyle="round")
            ax.fill_between(xs, ys, 0, step="post", color=color, alpha=0.10, zorder=2, linewidth=0)
            dots = [(x, y, y > before) for x, y, before in zip(xs[1:], ys[1:], ys[:-1])]
            ax.scatter(
                [d[0] for d in dots], [d[1] for d in dots], s=32, zorder=4, edgecolors=color, linewidths=1.4,
                c=[WIN if d[2] else LOSS for d in dots],
            )
        else:  # длинная история: гладкая линия без точек
            ax.plot(xs, ys, color=color, linewidth=2.2, label=label, zorder=3, solid_joinstyle="round")
            ax.fill_between(xs, ys, 0, color=color, alpha=0.10, zorder=2, linewidth=0)
        ax.annotate(
            f"{total:+d}", (xs[-1], ys[-1]), xytext=(8, 0), textcoords="offset points", va="center",
            color=color, fontsize=11, fontweight="bold", zorder=6,
            bbox={"facecolor": PANEL, "edgecolor": "none", "pad": 1.5, "alpha": 0.85},
        )
        if len(ordered) == 1 and games >= 3:  # у одного игрока подписываем пик и просадку
            full_ys = [value for _, value in points]
            hi, lo = max(full_ys), min(full_ys)
            hi_ts, lo_ts = points[full_ys.index(hi)][0], points[full_ys.index(lo)][0]
            if hi > 0:
                ax.annotate(f"▲ пик {hi:+d}", (to_dt(hi_ts), hi), xytext=(0, 10), textcoords="offset points",
                            ha="center", color=WIN, fontsize=9, zorder=6)
            if lo < 0:
                ax.annotate(f"▼ просадка {lo:+d}", (to_dt(lo_ts), lo), xytext=(0, -16), textcoords="offset points",
                            ha="center", color=LOSS, fontsize=9, zorder=6)

    fig.text(0.075, 0.935, title, color=FG, fontsize=16, fontweight="bold", ha="left")
    ax.set_ylabel("± MMR (оценка)", color=MUTED, fontsize=10)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=8))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}" if v else "0"))
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    locator = mdates.AutoDateLocator(tz=tz)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=tz))
    ax.margins(x=0.06)
    if since_ts is not None and until_ts is not None:  # ось — выбранный период целиком, а не только где есть игры
        ax.set_xlim(to_dt(since_ts), to_dt(until_ts + (until_ts - since_ts) // 25))

    legend = fig.legend(
        *ax.get_legend_handles_labels(), loc="lower left", bbox_to_anchor=(0.075, 0.02), ncol=1, frameon=False,
        fontsize=10.5, labelcolor=FG, handlelength=2.2,
    )
    for handle in legend.legend_handles:
        handle.set_linewidth(3)
    if any(len(pts) <= DOTS_LIMIT for _, pts in ordered):
        fig.text(0.975, 0.012, "точки: зелёная — победа, красная — поражение", color=MUTED, fontsize=8, ha="right")

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()
