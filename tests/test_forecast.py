from datetime import date, timedelta
from decimal import Decimal
from statistics import fmean

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cashflow.core.enums import Cadence, ForecastModel
from cashflow.core.forecast import (
    BacktestScore,
    History,
    backtest,
    build_forecast,
    choose_model,
)
from cashflow.core.models import Transaction
from cashflow.core.recurring import DetectedSeries
from cashflow.demo.generator import generate
from cashflow.settings import ForecastSettings, RecurringSettings

RECURRING = RecurringSettings()
FORECAST = ForecastSettings()
AS_OF = date(2026, 10, 8)


def history(seed: int, end: date = AS_OF, months: int = 24) -> History:
    ds = generate(seed, end, months)
    return History(ds.transactions, ds.entity.opening_balance, ds.entity.opening_balance_on)


def txn(posted_on: date, amount: str, vendor: str, n: int = 0) -> Transaction:
    return Transaction(
        entity_id="e",
        source_id=f"{vendor}-{posted_on}-{n}",
        account_id="a",
        posted_on=posted_on,
        amount=Decimal(amount),
        description="",
        vendor=vendor,
        category="c",
    )


# Recurring projection ------------------------------------------------------------------------


def test_month_end_series_projects_onto_its_anchor_day() -> None:
    series = DetectedSeries(
        "Rent", Cadence.MONTHLY, Decimal("-100"), 6, date(2026, 1, 31), date(2026, 2, 28), 31
    )

    dates = series.occurrences_between(date(2026, 2, 1), date(2026, 5, 31))

    assert dates == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)]


def test_overdue_occurrences_are_skipped_not_piled_up() -> None:
    series = DetectedSeries(
        "Slack", Cadence.MONTHLY, Decimal("-40"), 9, date(2026, 6, 3), date(2026, 7, 3), 3
    )

    dates = series.occurrences_between(date(2026, 10, 9), date(2026, 11, 30))

    assert dates == [date(2026, 11, 3)]


def test_purely_recurring_history_forecasts_exactly() -> None:
    """With no residual activity there is nothing to model: the forecast is the schedule."""
    start = date(2025, 1, 1)
    transactions = [
        txn(start + timedelta(days=14 * k), "-1000.00", "Payroll", k) for k in range(45)
    ] + [txn(date(2025 + (m // 12), m % 12 + 1, 5), "3000.00", "Retainer", m) for m in range(21)]
    h = History(transactions, Decimal("10000.00"), date(2024, 12, 31))
    as_of = date(2026, 9, 30)

    run = build_forecast(h, as_of=as_of, recurring=RECURRING, forecast=FORECAST)

    points = run.horizon(30).points
    assert all(p.lower == p.expected == p.upper for p in points)
    payroll_days = {start + timedelta(days=14 * k) for k in range(45, 50)}
    expected = run.starting_balance
    for p in points:
        expected += Decimal("-1000.00") if p.on_date in payroll_days else 0
        expected += Decimal("3000.00") if p.on_date.day == 5 else 0
        assert p.expected == expected


# Shape of every forecast -----------------------------------------------------------------------


@settings(max_examples=8, deadline=None)
@given(st.integers(min_value=0, max_value=10_000))
def test_forecast_bands_are_well_formed(seed: int) -> None:
    run = build_forecast(history(seed), as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    longest = run.horizon(FORECAST.max_horizon).points
    assert [p.on_date for p in longest] == [
        AS_OF + timedelta(days=d) for d in range(1, FORECAST.max_horizon + 1)
    ]
    assert all(p.lower <= p.expected <= p.upper for p in longest)
    widths = [p.upper - p.lower for p in longest]
    assert widths == sorted(widths), "band never narrows further out"
    assert all(p.expected == p.expected.quantize(Decimal("0.01")) for p in longest)
    for h in FORECAST.horizons_days:
        assert run.horizon(h).points == longest[:h], "horizons come from one fitted model"


def test_starting_balance_includes_everything_to_date() -> None:
    h = history(5)

    run = build_forecast(h, as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    assert run.starting_balance == h.opening_balance + sum(t.amount for t in h.transactions)


# Backtest and model choice ---------------------------------------------------------------------


def test_backtest_scores_every_candidate_at_every_horizon() -> None:
    scores = backtest(history(3), as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    assert set(scores) == set(FORECAST.candidate_models)
    for by_horizon in scores.values():
        assert set(by_horizon) == set(FORECAST.horizons_days)
        for score in by_horizon.values():
            assert score.mase is not None
            assert score.mase > 0
            assert 0 <= score.coverage <= 1
            assert score.balance_error >= 0


def test_live_forecast_uses_the_model_with_lowest_balance_error() -> None:
    h = history(3)
    scores = backtest(h, as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    run = build_forecast(h, as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    best = min(scores, key=lambda m: scores[m][FORECAST.max_horizon].balance_error)
    assert run.model is best
    assert run.horizon(30).backtest == scores[best][30]


def test_choice_ignores_mase_in_favour_of_balance_error() -> None:
    scores = {
        ForecastModel.AUTO_ETS: {90: BacktestScore(mase=0.9, coverage=0.8, balance_error=500)},
        ForecastModel.HISTORIC_AVERAGE: {
            90: BacktestScore(mase=0.4, coverage=0.8, balance_error=9_000)
        },
    }

    assert choose_model(scores, FORECAST) is ForecastModel.AUTO_ETS


def test_short_history_falls_back_without_a_backtest() -> None:
    run = build_forecast(history(1, months=6), as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    assert run.model is FORECAST.fallback_model
    assert all(h.backtest is None for h in run.horizons)


def test_no_transactions_forecasts_a_flat_opening_balance() -> None:
    h = History([], Decimal("500.00"), date(2026, 1, 1))

    run = build_forecast(h, as_of=AS_OF, recurring=RECURRING, forecast=FORECAST)

    assert {p.expected for p in run.horizon(30).points} == {Decimal("500.00")}


# Accuracy on held-out data ---------------------------------------------------------------------

HELD_OUT_SEEDS = range(20)


@pytest.fixture(scope="module")
def held_out() -> dict[int, dict[str, list[float]]]:
    """Forecast from 90 days before the end of 27 months of data; compare with what happened.

    "flat" is the naive alternative: assume the balance doesn't change.
    """
    results: dict[int, dict[str, list[float]]] = {
        h: {"error": [], "flat": [], "inside": []} for h in FORECAST.horizons_days
    }
    for seed in HELD_OUT_SEEDS:
        h = history(seed, months=27)
        cut = AS_OF - timedelta(days=90)
        run = build_forecast(h, as_of=cut, recurring=RECURRING, forecast=FORECAST)
        for days in FORECAST.horizons_days:
            point = run.horizon(days).points[-1]
            actual = h.balance_at(point.on_date)
            results[days]["error"].append(float(abs(actual - point.expected)))
            results[days]["flat"].append(float(abs(actual - run.starting_balance)))
            results[days]["inside"].append(float(point.lower <= actual <= point.upper))
    return results


@pytest.mark.parametrize(("days", "max_ratio"), [(60, 1.0), (90, 0.75)])
def test_forecast_beats_a_flat_balance_further_out(
    held_out: dict[int, dict[str, list[float]]], days: int, max_ratio: float
) -> None:
    """At 30 days lumpy client revenue dominates and a flat guess is as good, so only 60 and
    90 days are asserted. Measured on these seeds: 0.98x and 0.58x the flat error. The 60-day
    margin is thin; it is deterministic, but a generator change can legitimately move it."""
    errors = held_out[days]

    assert fmean(errors["error"]) < max_ratio * fmean(errors["flat"])


@pytest.mark.parametrize("days", FORECAST.horizons_days)
def test_real_balance_usually_lands_inside_the_band(
    held_out: dict[int, dict[str, list[float]]], days: int
) -> None:
    assert fmean(held_out[days]["inside"]) >= 0.7
