"""Render an entity's stored forecast as light and dark PNGs for the README.

    uv run --group docs python scripts/forecast_chart.py [--entity demo-42] [--history-days 180]

Writes docs/images/forecast-light.png and forecast-dark.png; the README picks one with
<picture> by the reader's color scheme. Colors are the dataviz reference palette's first two
categorical slots, validated (CVD and contrast) against each mode's surface.
"""

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import accumulate
from pathlib import Path

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from cashflow.core.forecast import History
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.db.session import make_engine, make_session_factory
from cashflow.settings import get_settings

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "images"
EPOCH = date(1970, 1, 1)  # matplotlib's default date epoch (rcParams["date.epoch"])


def _x(day: date) -> float:
    """matplotlib's numeric x for a date: days since its epoch (what date2num returns)."""
    return float((day - EPOCH).days)


@dataclass(frozen=True)
class Theme:
    name: str
    surface: str
    ink: str
    secondary: str
    muted: str
    grid: str
    baseline: str
    actual: str
    forecast: str


LIGHT = Theme(
    "light", "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834"
)
DARK = Theme(
    "dark", "#1a1a19", "#ffffff", "#c3c2b7", "#898781", "#2c2c2a", "#383835", "#3987e5", "#d95926"
)


@dataclass(frozen=True)
class Series:
    entity_name: str
    model: str
    typical_error: Decimal | None
    today: date
    actual_days: list[date]
    actual: list[float]
    forecast_days: list[date]
    expected: list[float]
    lower: list[float]
    upper: list[float]


def load(entity_id: str, history_days: int) -> Series:
    engine = make_engine(get_settings())
    try:
        with make_session_factory(engine)() as session:
            entity = session.get(orm.Entity, entity_id)
            history = repositories.entity_history(session, entity_id)
            forecast = repositories.latest_forecast(session, entity_id, 90)
            if entity is None or history is None or forecast is None:
                raise SystemExit(f"{entity_id}: no such entity, or no 90-day forecast yet")
            today = forecast.as_of
            days, balances = _daily_balances(history, today - timedelta(history_days), today)
            return Series(
                entity_name=entity.name,
                model=forecast.model,
                typical_error=forecast.backtest_balance_error,
                today=today,
                actual_days=days,
                actual=balances,
                forecast_days=[today, *(p.on_date for p in forecast.points)],
                expected=[balances[-1], *(float(p.expected_balance) for p in forecast.points)],
                lower=[balances[-1], *(float(p.lower) for p in forecast.points)],
                upper=[balances[-1], *(float(p.upper) for p in forecast.points)],
            )
    finally:
        engine.dispose()


def _daily_balances(history: History, start: date, end: date) -> tuple[list[date], list[float]]:
    flow: dict[date, Decimal] = {}
    for t in history.transactions:
        if start < t.posted_on <= end:
            flow[t.posted_on] = flow.get(t.posted_on, Decimal(0)) + t.amount
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    balances = accumulate(
        (flow.get(d, Decimal(0)) for d in days[1:]), initial=history.balance_at(start)
    )
    return days, [float(b) for b in balances]


def _dollars(value: float, _: object = None) -> str:
    return f"${value / 1000:,.0f}k"


def render(series: Series, theme: Theme, path: Path) -> None:
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 11})
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=160)
    fig.patch.set_facecolor(theme.surface)
    ax.set_facecolor(theme.surface)
    actual_x = [_x(d) for d in series.actual_days]
    forecast_x = [_x(d) for d in series.forecast_days]

    # Band first so the lines sit on top of it; the series hue at ~10% opacity, never solid.
    ax.fill_between(
        forecast_x,
        series.lower,
        series.upper,
        color=theme.forecast,
        alpha=0.12,
        linewidth=0,
        label="80% band",
    )
    ax.plot(
        actual_x,
        series.actual,
        color=theme.actual,
        linewidth=2,
        solid_capstyle="round",
        label="Actual balance",
    )
    ax.plot(
        forecast_x,
        series.expected,
        color=theme.forecast,
        linewidth=2,
        solid_capstyle="round",
        label="Expected balance",
    )

    # Today: a solid hairline in the baseline color (dashes would read as a threshold).
    ax.axvline(_x(series.today), color=theme.baseline, linewidth=1, zorder=0)
    ax.annotate(
        "Today",
        (_x(series.today), 1),
        xycoords=("data", "axes fraction"),
        xytext=(4, -4),
        textcoords="offset points",
        va="top",
        color=theme.muted,
        fontsize=10,
    )

    # The low point: what an owner actually asks ("will I run short, and when?").
    low = min(range(1, len(series.expected)), key=lambda i: series.expected[i])
    low_day, low_value = series.forecast_days[low], series.expected[low]
    ax.scatter(
        [_x(low_day)],
        [low_value],
        s=64,
        color=theme.forecast,
        edgecolors=theme.surface,
        linewidths=2,
        zorder=5,
    )
    ax.annotate(
        f"Lowest expected: {_dollars(low_value)} on {low_day:%b} {low_day.day}",
        (_x(low_day), low_value),
        xytext=(10, -18),
        textcoords="offset points",
        color=theme.ink,
        fontsize=10,
    )

    # Direct labels at the line ends, in ink rather than the series color.
    ax.annotate(
        "Actual",
        (actual_x[-1], max(series.actual[-14:])),
        xytext=(-8, 10),
        textcoords="offset points",
        ha="right",
        color=theme.secondary,
        fontsize=10,
    )
    ax.annotate(
        "Expected",
        (forecast_x[-1], series.expected[-1]),
        xytext=(-6, 8),
        textcoords="offset points",
        ha="right",
        color=theme.secondary,
        fontsize=10,
    )

    error = (
        f" · typical backtest error {_dollars(float(series.typical_error))}"
        if series.typical_error
        else ""
    )
    fig.suptitle(
        f"{series.entity_name}: 90-day cash forecast",
        x=0.06,
        y=0.97,
        ha="left",
        color=theme.ink,
        fontsize=15,
        fontweight="bold",
    )
    fig.text(
        0.06,
        0.905,
        f"Daily balance with an 80% band · model {series.model}{error}",
        ha="left",
        color=theme.secondary,
        fontsize=10.5,
    )

    ax.yaxis.set_major_formatter(FuncFormatter(_dollars))
    # matplotlib.dates ships without type annotations.
    ax.xaxis.set_major_locator(mdates.MonthLocator())  # type: ignore[no-untyped-call]
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))  # type: ignore[no-untyped-call]
    ax.grid(axis="y", color=theme.grid, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(theme.baseline)
    ax.tick_params(colors=theme.muted, length=0, labelsize=10)

    legend = ax.legend(loc="upper left", frameon=False, fontsize=10, ncols=3)
    for text in legend.get_texts():
        text.set_color(theme.secondary)

    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.09)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=theme.surface)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--entity", default="demo-42")
    parser.add_argument("--history-days", type=int, default=180)
    args = parser.parse_args()

    series = load(args.entity, args.history_days)
    for theme in (LIGHT, DARK):
        path = OUT_DIR / f"forecast-{theme.name}.png"
        render(series, theme, path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
