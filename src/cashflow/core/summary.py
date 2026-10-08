"""Weekly summary: how last week went, and what's coming.

`build_summary` is pure: callers load the inputs (history, stored forecast, open anomalies)
and get back a Pydantic model that the API and MCP layers return as is.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import polars as pl
from pydantic import BaseModel

from cashflow.core.enums import AnomalyType, Cadence, Severity
from cashflow.core.forecast import BalancePoint, History
from cashflow.core.recurring import DetectedSeries

CENT = Decimal("0.01")
WEEK = 7
COMPARISON_WEEKS = 4
TOP_CATEGORIES = 5
TOP_ANOMALIES = 5
UPCOMING_DAYS = 7
SEVERITY_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


# Inputs ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredForecast:
    as_of: date
    model: str
    points: Sequence[BalancePoint]
    """The longest horizon's daily points; shorter horizons are its prefixes."""
    typical_error: dict[int, Decimal | None]
    """Backtest balance error by horizon."""


@dataclass(frozen=True)
class OpenAnomaly:
    id: int
    type: AnomalyType
    severity: Severity
    explanation: str
    detected_at: datetime


# Output ----------------------------------------------------------------------------------------


class CashFlow(BaseModel):
    money_in: Decimal
    money_out: Decimal
    net: Decimal


class WeekCash(BaseModel):
    start_balance: Decimal
    end_balance: Decimal
    this_week: CashFlow
    previous_week: CashFlow
    four_week_average: CashFlow


class CategorySpend(BaseModel):
    """Discretionary (non-recurring) spend in one category.

    No percentage change: many categories bill monthly (cloud hosting, a quarterly order), so
    in a weekly window they swing between nothing and everything, and a ratio would mislead."""

    category: str
    spent: Decimal
    four_week_average: Decimal


class UpcomingItem(BaseModel):
    on_date: date
    vendor: str
    cadence: Cadence
    amount: Decimal
    """Signed: negative is a bill, positive is income."""


class OutlookHorizon(BaseModel):
    horizon_days: int
    on_date: date
    expected: Decimal
    lower: Decimal
    upper: Decimal
    typical_error: Decimal | None


class LowPoint(BaseModel):
    on_date: date
    balance: Decimal


class Outlook(BaseModel):
    as_of: date
    model: str
    horizons: list[OutlookHorizon]
    lowest_expected: LowPoint
    """The lowest expected balance within the forecast: "will I run short, and when?"."""
    lowest_lower_band: LowPoint


class AnomalyItem(BaseModel):
    id: int
    type: AnomalyType
    severity: Severity
    explanation: str
    detected_at: datetime
    new: bool
    """Detected since the start of the summary week."""


class AnomalyDigest(BaseModel):
    open_by_severity: dict[Severity, int]
    new_count: int
    top: list[AnomalyItem]


class WeeklySummary(BaseModel):
    entity_id: str
    entity_name: str
    currency: str
    today: date
    week_start: date
    week_end: date
    cash: WeekCash
    top_discretionary_categories: list[CategorySpend]
    """Where non-recurring money went; recurring bills are in `upcoming` and the anomalies."""
    upcoming: list[UpcomingItem]
    outlook: Outlook | None
    """None until the entity's first forecast."""
    anomalies: AnomalyDigest
    refresh_pending: bool
    """New transactions are waiting for a refresh, so outlook and anomalies may lag."""


# Building --------------------------------------------------------------------------------------


def summary_week(today: date, week_of: date | None = None) -> tuple[date, date]:
    """Monday-Sunday. Default: the last complete week, so on a Monday the one just ended."""
    if week_of is not None:
        start = week_of - timedelta(days=week_of.weekday())
    else:
        start = today - timedelta(days=today.weekday() + WEEK)
    return start, start + timedelta(days=WEEK - 1)


@dataclass(frozen=True)
class EntityInfo:
    id: str
    name: str
    currency: str


def build_summary(
    entity: EntityInfo,
    history: History,
    series: Sequence[DetectedSeries],
    forecast: StoredForecast | None,
    anomalies: Sequence[OpenAnomaly],
    *,
    today: date,
    week_start: date,
    refresh_pending: bool,
) -> WeeklySummary:
    week_end = week_start + timedelta(days=WEEK - 1)
    return WeeklySummary(
        entity_id=entity.id,
        entity_name=entity.name,
        currency=entity.currency,
        today=today,
        week_start=week_start,
        week_end=week_end,
        cash=_cash(history, week_start),
        top_discretionary_categories=_top_discretionary(history, series, week_start),
        upcoming=_upcoming(series, today),
        outlook=_outlook(forecast) if forecast else None,
        anomalies=_anomalies(anomalies, week_start),
        refresh_pending=refresh_pending,
    )


def _flow(history: History, start: date) -> CashFlow:
    week = [
        t.amount for t in history.transactions if start <= t.posted_on < start + timedelta(WEEK)
    ]
    money_in = sum((a for a in week if a > 0), Decimal(0))
    money_out = -sum((a for a in week if a < 0), Decimal(0))
    return CashFlow(money_in=money_in, money_out=money_out, net=money_in - money_out)


def _cash(history: History, start: date) -> WeekCash:
    earlier = [_flow(history, start - timedelta(weeks=k)) for k in range(1, COMPARISON_WEEKS + 1)]
    return WeekCash(
        start_balance=history.balance_at(start - timedelta(days=1)),
        end_balance=history.balance_at(start + timedelta(days=WEEK - 1)),
        this_week=_flow(history, start),
        previous_week=earlier[0],
        four_week_average=CashFlow(
            money_in=_average(f.money_in for f in earlier),
            money_out=_average(f.money_out for f in earlier),
            net=_average(f.net for f in earlier),
        ),
    )


def _top_discretionary(
    history: History, series: Sequence[DetectedSeries], start: date
) -> list[CategorySpend]:
    recurring = {s.vendor for s in series if s.typical_amount < 0}
    window_start = start - timedelta(weeks=COMPARISON_WEEKS)
    rows = [
        (t.category, (t.posted_on - start).days // WEEK, -t.amount)
        for t in history.transactions
        if t.amount < 0
        and t.vendor not in recurring
        and window_start <= t.posted_on < start + timedelta(WEEK)
    ]
    if not rows:
        return []
    # Week 0 is the summary week; -1 to -4 are the comparison weeks before it.
    totals = (
        pl.DataFrame(
            rows,
            schema={"category": pl.String, "week": pl.Int64, "spent": pl.Decimal(14, 2)},
            orient="row",
        )
        .group_by("category")
        .agg(
            this_week=pl.col("spent").filter(pl.col("week") == 0).sum(),
            earlier=pl.col("spent").filter(pl.col("week") < 0).sum(),
        )
        .filter(pl.col("this_week") > 0)
        .sort(["this_week", "category"], descending=[True, False])
        .head(TOP_CATEGORIES)
    )
    return [
        CategorySpend(
            category=category,
            spent=Decimal(spent),
            four_week_average=(Decimal(earlier) / COMPARISON_WEEKS).quantize(CENT),
        )
        for category, spent, earlier in totals.iter_rows()
    ]


def _upcoming(series: Sequence[DetectedSeries], today: date) -> list[UpcomingItem]:
    """Scheduled in the next week. Overdue charges are skipped: they're anomalies, not plans."""
    window = (today + timedelta(days=1), today + timedelta(days=UPCOMING_DAYS))
    items = [
        UpcomingItem(on_date=day, vendor=s.vendor, cadence=s.cadence, amount=s.typical_amount)
        for s in series
        for day in s.occurrences_between(*window)
    ]
    return sorted(items, key=lambda i: (i.on_date, i.vendor))


def _outlook(forecast: StoredForecast) -> Outlook:
    points = list(forecast.points)
    lowest = min(points, key=lambda p: (p.expected, p.on_date))
    lowest_band = min(points, key=lambda p: (p.lower, p.on_date))
    return Outlook(
        as_of=forecast.as_of,
        model=forecast.model,
        horizons=[
            OutlookHorizon(
                horizon_days=h,
                on_date=points[h - 1].on_date,
                expected=points[h - 1].expected,
                lower=points[h - 1].lower,
                upper=points[h - 1].upper,
                typical_error=error,
            )
            for h, error in sorted(forecast.typical_error.items())
            if h <= len(points)
        ],
        lowest_expected=LowPoint(on_date=lowest.on_date, balance=lowest.expected),
        lowest_lower_band=LowPoint(on_date=lowest_band.on_date, balance=lowest_band.lower),
    )


def _anomalies(anomalies: Sequence[OpenAnomaly], week_start: date) -> AnomalyDigest:
    ranked = sorted(
        anomalies,
        key=lambda a: (SEVERITY_RANK[a.severity], -a.detected_at.timestamp(), -a.id),
    )
    is_new = {a.id: a.detected_at.date() >= week_start for a in anomalies}
    return AnomalyDigest(
        open_by_severity={s: sum(a.severity is s for a in anomalies) for s in SEVERITY_RANK},
        new_count=sum(is_new.values()),
        top=[
            AnomalyItem(
                id=a.id,
                type=a.type,
                severity=a.severity,
                explanation=a.explanation,
                detected_at=a.detected_at,
                new=is_new[a.id],
            )
            for a in ranked[:TOP_ANOMALIES]
        ],
    )


def _average(values: Iterable[Decimal]) -> Decimal:
    items = list(values)
    return (sum(items, Decimal(0)) / len(items)).quantize(CENT)
