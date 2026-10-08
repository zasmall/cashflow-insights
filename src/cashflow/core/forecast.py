"""Balance forecast: known recurring flows plus a modelled residual, with honest backtesting.

For a cutoff date, the pipeline is:

1. Detect recurring series in history up to the cutoff and project them over the horizon.
   These "known" flows are treated as certain.
2. Sum everything else per day (the residual) and forecast it with a statsforecast model,
   which also gives a per-day prediction interval.
3. Expected balance = starting balance + cumulative (known + residual mean). The model's
   one-step interval gives a daily standard deviation; days are treated as independent and
   equally uncertain, so the balance band widens with the square root of days ahead.

The backtest runs the identical pipeline at past cutoffs, on only the data before each cutoff
(recurring detection included), then scores it against what really happened. The candidate
model with the lowest balance error is used for the live forecast.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import accumulate
from statistics import NormalDist, fmean

import polars as pl

from cashflow.core.enums import ForecastModel
from cashflow.core.forecast_models import ModelForecast, fit_predict
from cashflow.core.models import Transaction
from cashflow.core.recurring import detect_recurring
from cashflow.settings import ForecastSettings, RecurringSettings

CENT = Decimal("0.01")
WEEK = 7


@dataclass(frozen=True)
class History:
    """Everything known about one entity's cash."""

    transactions: Sequence[Transaction]
    opening_balance: Decimal
    opening_balance_on: date

    def balance_at(self, day: date) -> Decimal:
        return self.opening_balance + sum(
            (t.amount for t in self.transactions if self.opening_balance_on < t.posted_on <= day),
            Decimal(0),
        )


@dataclass(frozen=True)
class BacktestScore:
    mase: float | None
    """Mean absolute error of daily net flow, scaled by a same-weekday-last-week guess on the
    training data. Below 1 beats that naive guess. None when the scale is zero."""
    coverage: float
    """Share of backtest days on which the real balance stayed inside the band."""
    balance_error: float
    """Mean absolute gap between forecast and real balance over the horizon, in currency.
    This is what a reader of the forecast experiences, so models are chosen on it."""


@dataclass(frozen=True)
class BalancePoint:
    on_date: date
    expected: Decimal
    lower: Decimal
    upper: Decimal


@dataclass(frozen=True)
class HorizonForecast:
    horizon_days: int
    backtest: BacktestScore | None
    points: tuple[BalancePoint, ...]


@dataclass(frozen=True)
class ForecastRun:
    as_of: date
    starting_balance: Decimal
    model: ForecastModel
    horizons: tuple[HorizonForecast, ...]

    def horizon(self, days: int) -> HorizonForecast:
        return next(h for h in self.horizons if h.horizon_days == days)


Scores = dict[ForecastModel, dict[int, BacktestScore]]


def build_forecast(
    history: History, *, as_of: date, recurring: RecurringSettings, forecast: ForecastSettings
) -> ForecastRun:
    scores = backtest(history, as_of=as_of, recurring=recurring, forecast=forecast)
    model = choose_model(scores, forecast)

    horizon = forecast.max_horizon
    inputs = _inputs(history, as_of, horizon, recurring)
    fitted = fit_predict(
        inputs.residual, horizon=horizon, level=forecast.confidence_level, models=[model]
    )
    expected, lower, upper = _band(inputs, fitted[model], _z(forecast.confidence_level))
    points = tuple(
        BalancePoint(
            on_date=as_of + timedelta(days=i + 1),
            expected=_cents(expected[i]),
            lower=_cents(lower[i]),
            upper=_cents(upper[i]),
        )
        for i in range(horizon)
    )
    return ForecastRun(
        as_of=as_of,
        starting_balance=history.balance_at(as_of),
        model=model,
        horizons=tuple(
            HorizonForecast(h, scores.get(model, {}).get(h), points[:h])
            for h in sorted(forecast.horizons_days)
        ),
    )


def choose_model(scores: Scores, settings: ForecastSettings) -> ForecastModel:
    """The candidate with the lowest backtest balance error at the longest horizon.

    Not MASE: daily-flow error rewards copying last week (SeasonalNaive), whose errors then
    compound in the balance. On the demo data that model had the best MASE for some entities
    but a 39% median balance error at 90 days, against 6% for the others.
    """
    horizon = settings.max_horizon
    scored = [
        (score.balance_error, model)
        for model in settings.candidate_models
        if (score := scores.get(model, {}).get(horizon)) is not None
    ]
    return min(scored)[1] if scored else settings.fallback_model


def backtest(
    history: History, *, as_of: date, recurring: RecurringSettings, forecast: ForecastSettings
) -> Scores:
    """Rolling-origin evaluation of every candidate. Empty when history is too short."""
    if not history.transactions:
        return {}
    horizon = forecast.max_horizon
    z = _z(forecast.confidence_level)
    first_day = min(t.posted_on for t in history.transactions)
    cutoffs = [
        cutoff
        for k in range(forecast.backtest_windows)
        if (cutoff := as_of - timedelta(days=horizon + k * forecast.backtest_step_days)) - first_day
        >= timedelta(days=forecast.min_training_days)
    ]

    mase: dict[tuple[ForecastModel, int], list[float]] = {}
    coverage: dict[tuple[ForecastModel, int], list[float]] = {}
    balance_error: dict[tuple[ForecastModel, int], list[float]] = {}
    for cutoff in cutoffs:
        inputs = _inputs(history, cutoff, horizon, recurring)
        window_end = cutoff + timedelta(days=horizon)
        actual_flow = _daily(
            [t for t in history.transactions if cutoff < t.posted_on <= window_end],
            cutoff + timedelta(days=1),
            window_end,
        )["y"].to_list()
        actual_balance = list(accumulate(actual_flow, initial=inputs.start_balance))[1:]
        fitted = fit_predict(
            inputs.residual,
            horizon=horizon,
            level=forecast.confidence_level,
            models=forecast.candidate_models,
        )
        for model, residual in fitted.items():
            predicted_flow = [k + r for k, r in zip(inputs.known, residual.mean, strict=True)]
            expected, lower, upper = _band(inputs, residual, z)
            for h in forecast.horizons_days:
                errors = [
                    abs(a - p) for a, p in zip(actual_flow[:h], predicted_flow[:h], strict=True)
                ]
                if inputs.scale:
                    mase.setdefault((model, h), []).append(fmean(errors) / inputs.scale)
                inside = [
                    lo <= bal <= hi
                    for lo, bal, hi in zip(lower[:h], actual_balance[:h], upper[:h], strict=True)
                ]
                coverage.setdefault((model, h), []).append(fmean(inside))
                gaps = [abs(a - e) for a, e in zip(actual_balance[:h], expected[:h], strict=True)]
                balance_error.setdefault((model, h), []).append(fmean(gaps))

    return {
        model: {
            h: BacktestScore(
                mase=fmean(mase[(model, h)]) if (model, h) in mase else None,
                coverage=fmean(coverage[(model, h)]),
                balance_error=fmean(balance_error[(model, h)]),
            )
            for h in forecast.horizons_days
        }
        for model in forecast.candidate_models
        if cutoffs
    }


@dataclass(frozen=True)
class _Inputs:
    """What the pipeline knows at a cutoff."""

    start_balance: float
    known: list[float]
    """Projected recurring flow for each day of the horizon."""
    residual: pl.DataFrame
    """Daily non-recurring net flow (`ds`, `y`) up to the cutoff, without gaps."""
    scale: float | None
    """MASE denominator: mean |y[t] - y[t-7]| of total daily net flow before the cutoff."""


def _inputs(history: History, cutoff: date, horizon: int, recurring: RecurringSettings) -> _Inputs:
    past = [t for t in history.transactions if t.posted_on <= cutoff]
    series = detect_recurring(past, as_of=cutoff, settings=recurring)

    known = [0.0] * horizon
    for s in series:
        for day in s.occurrences_between(cutoff + timedelta(days=1), cutoff + timedelta(horizon)):
            known[(day - cutoff).days - 1] += float(s.typical_amount)

    # A vendor's whole flow in a direction is "known" once it has a series, matching detection.
    recurring_keys = {(s.vendor, s.typical_amount > 0) for s in series}
    residual = [t for t in past if (t.vendor, t.amount > 0) not in recurring_keys]
    first_day = min((t.posted_on for t in past), default=cutoff)
    totals = _daily(past, first_day, cutoff)["y"]

    return _Inputs(
        start_balance=float(history.balance_at(cutoff)),
        known=known,
        residual=_daily(residual, first_day, cutoff),
        scale=_seasonal_naive_mae(totals),
    )


def _daily(transactions: Sequence[Transaction], start: date, end: date) -> pl.DataFrame:
    """Net flow per calendar day in [start, end], with zero for days without transactions."""
    days = pl.DataFrame({"ds": pl.date_range(start, end, "1d", eager=True)})
    flows = pl.DataFrame(
        [(t.posted_on, float(t.amount)) for t in transactions],
        schema={"ds": pl.Date, "y": pl.Float64},
        orient="row",
    )
    return (
        days.join(flows.group_by("ds").agg(pl.col("y").sum()), on="ds", how="left")
        .with_columns(pl.col("y").fill_null(0.0))
        .sort("ds")
    )


def _seasonal_naive_mae(series: pl.Series) -> float | None:
    if series.len() <= WEEK:
        return None
    scale = (series - series.shift(WEEK)).abs().drop_nulls().mean()
    return float(scale) if isinstance(scale, float) and scale > 0 else None


def _band(
    inputs: _Inputs, residual: ModelForecast, z: float
) -> tuple[list[float], list[float], list[float]]:
    """Expected balance and its band for each day of the horizon.

    Each day's residual flow is given the spread of the model's one-step-ahead interval, and
    those spreads add in quadrature over the days. The model's own later intervals already
    accumulate uncertainty, so summing them would count it twice; backtests showed that
    overshooting to about 93% coverage for an 80% band, against about 75-84% this way.
    """
    flow = [k + r for k, r in zip(inputs.known, residual.mean, strict=True)]
    expected = list(accumulate(flow, initial=inputs.start_balance))[1:]
    daily_sd = (residual.upper[0] - residual.lower[0]) / (2 * z) if residual.mean else 0.0
    spread = [z * daily_sd * math.sqrt(day) for day in range(1, len(flow) + 1)]
    return (
        expected,
        [e - s for e, s in zip(expected, spread, strict=True)],
        [e + s for e, s in zip(expected, spread, strict=True)],
    )


def _z(level: int) -> float:
    return NormalDist().inv_cdf(0.5 + level / 200)


def _cents(value: float) -> Decimal:
    return Decimal(value).quantize(CENT)
