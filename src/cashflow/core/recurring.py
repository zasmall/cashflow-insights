"""Detect recurring bills and income in a transaction history.

One candidate series per (vendor, direction). A candidate is recurring when:

1. **Amounts are stable:** at least `min_consistent_share` of charges are within
   `amount_tolerance` of the median. The median is the typical amount, so a recent price
   change leaves the series intact (and visible to the anomaly scan) until it becomes the norm.
2. **Timing is regular:** the median gap picks the nearest cadence, and at least
   `min_consistent_share` of gaps match that cadence's calendar step within
   `cadence_tolerance_days`. One skipped charge doesn't break a series.
3. **There is enough evidence:** at least `min_occurrences[cadence]` charges. When that is only
   two (annual), both amounts must be identical.
4. **It hasn't ended:** no more than `max_missed_cycles` expected charges have been missed.

Price changes: the typical amount stays the median, so the anomaly scan can compare against it.
Looking forward, though, a change confirmed by `CONFIRMING_CHARGES` consecutive charges at a
new price (outside `amount_tolerance` of typical, within it of each other) is projected at the
latest amount. One odd charge, like a prorated bill, moves nothing until a second confirms it.

Known limitation: two subscriptions from one vendor (say $89.99 and $22.99 monthly) split the
amounts so neither reaches the stable share, and neither is reported.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Any

import polars as pl

from cashflow.core.calendar import add_months, days_in_month
from cashflow.core.enums import Cadence
from cashflow.core.models import Transaction
from cashflow.settings import RecurringSettings

CENT = Decimal("0.01")
CONFIRMING_CHARGES = 2
AVERAGE_MONTH_DAYS = 365.25 / 12

NOMINAL_DAYS: dict[Cadence, float] = {
    Cadence.WEEKLY: 7,
    Cadence.BIWEEKLY: 14,
    Cadence.MONTHLY: AVERAGE_MONTH_DAYS,
    Cadence.QUARTERLY: 3 * AVERAGE_MONTH_DAYS,
    Cadence.ANNUAL: 365.25,
}
_STEP_DAYS = {Cadence.WEEKLY: 7, Cadence.BIWEEKLY: 14}
_STEP_MONTHS = {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.ANNUAL: 12}


@dataclass(frozen=True)
class DetectedSeries:
    vendor: str
    cadence: Cadence
    typical_amount: Decimal
    """Signed, like transactions: negative for bills, positive for income."""
    occurrences: int
    last_seen_on: date
    next_expected_on: date
    anchor_day: int | None
    """Day of month for calendar cadences; None for weekly and biweekly."""
    changed_amount: Decimal | None = None
    """A confirmed new price, when the latest charges have moved away from `typical_amount`."""

    @property
    def projected_amount(self) -> Decimal:
        """What future charges should be expected to cost."""
        return self.typical_amount if self.changed_amount is None else self.changed_amount

    def occurrences_between(self, start: date, end: date) -> list[date]:
        """Scheduled dates in [start, end]. Overdue charges before `start` are skipped."""
        anchor = self.anchor_day or self.next_expected_on.day
        dates: list[date] = []
        day = self.next_expected_on
        while day <= end:
            if day >= start:
                dates.append(day)
            day = next_occurrence(self.cadence, day, anchor)
        return dates


def next_occurrence(cadence: Cadence, after: date, anchor_day: int) -> date:
    """The next expected date. Calendar cadences land on `anchor_day`, clamped to month end."""
    if cadence in _STEP_DAYS:
        return after + timedelta(days=_STEP_DAYS[cadence])
    target = add_months(after.replace(day=1), _STEP_MONTHS[cadence])
    return target.replace(day=min(anchor_day, days_in_month(target.year, target.month)))


def detect_recurring(
    transactions: Iterable[Transaction], *, as_of: date, settings: RecurringSettings
) -> list[DetectedSeries]:
    candidates = _candidates(_frame(transactions), settings)
    detected = (_classify(row, as_of, settings) for row in candidates.iter_rows(named=True))
    return sorted(
        (s for s in detected if s is not None), key=lambda s: (s.vendor, s.typical_amount)
    )


def _frame(transactions: Iterable[Transaction]) -> pl.DataFrame:
    # Integer cents keep amounts exact; Decimal never needs to enter Polars.
    rows = [(t.vendor, t.posted_on, int(t.amount / CENT)) for t in transactions if t.amount]
    return pl.DataFrame(
        rows,
        schema={"vendor": pl.String, "posted_on": pl.Date, "cents": pl.Int64},
        orient="row",
    )


def _candidates(frame: pl.DataFrame, settings: RecurringSettings) -> pl.DataFrame:
    """One row per (vendor, direction) with the statistics the rules need."""
    keys = ["vendor", "direction"]
    # A float ratio is fine here: it only decides whether an amount counts as "close".
    tolerance = float(settings.amount_tolerance)
    median = pl.col("cents").median().over(keys)
    return (
        frame.with_columns(direction=pl.col("cents").sign())
        .sort("posted_on", "cents")
        .with_columns(
            median_cents=median,
            amount_fits=(pl.col("cents") - median).abs() <= median.abs() * tolerance,
            gap_days=pl.col("posted_on").diff().over(keys).dt.total_days(),
        )
        .group_by(keys, maintain_order=True)
        .agg(
            pl.col("median_cents").first(),
            amount_share=pl.col("amount_fits").mean(),
            median_gap=pl.col("gap_days").median(),
            dates=pl.col("posted_on"),
            amounts=pl.col("cents"),
            distinct_amounts=pl.col("cents").n_unique(),
        )
        .filter(pl.col("amount_share") >= settings.min_consistent_share)
    )


def _classify(
    row: dict[str, Any], as_of: date, settings: RecurringSettings
) -> DetectedSeries | None:
    dates: list[date] = row["dates"]
    median_gap: float | None = row["median_gap"]
    if median_gap is None:  # a single charge
        return None

    cadence = min(Cadence, key=lambda c: abs(NOMINAL_DAYS[c] - median_gap))
    if len(dates) < settings.min_occurrences[cadence]:
        return None
    if len(dates) == 2 and row["distinct_amounts"] > 1:  # noqa: PLR2004
        # Two charges is thin evidence (allowed for annual cadences): demand an exact repeat,
        # as real subscriptions bill, so two similar one-off purchases a year apart don't count.
        return None

    anchor_day = _anchor_day(dates)
    fitting_gaps = sum(
        abs((later - earlier).days - (next_occurrence(cadence, earlier, anchor_day) - earlier).days)
        <= settings.cadence_tolerance_days
        for earlier, later in pairwise(dates)
    )
    if fitting_gaps / (len(dates) - 1) < settings.min_consistent_share:
        return None

    next_expected = next_occurrence(cadence, dates[-1], anchor_day)
    if _missed_cycles(cadence, next_expected, as_of) > settings.max_missed_cycles:
        return None

    changed = _confirmed_change(row["amounts"], row["median_cents"], settings)
    return DetectedSeries(
        vendor=row["vendor"],
        cadence=cadence,
        typical_amount=(Decimal(row["median_cents"]) * CENT).quantize(CENT, ROUND_HALF_EVEN),
        occurrences=len(dates),
        last_seen_on=dates[-1],
        next_expected_on=next_expected,
        anchor_day=None if cadence in _STEP_DAYS else anchor_day,
        changed_amount=None if changed is None else Decimal(changed) * CENT,
    )


def _confirmed_change(
    amounts: list[int], median_cents: float, settings: RecurringSettings
) -> int | None:
    """The latest amount, if the newest charges agree on a price outside tolerance of typical."""
    tolerance = float(settings.amount_tolerance)
    changed: list[int] = []
    for cents in reversed(amounts):
        if abs(cents - median_cents) <= abs(median_cents) * tolerance:
            break
        changed.append(cents)
    if len(changed) < CONFIRMING_CHARGES:
        return None
    latest = changed[0]
    if any(abs(cents - latest) > abs(latest) * tolerance for cents in changed):
        return None  # the new charges disagree with each other: no single new price yet
    return latest


def _anchor_day(dates: list[date]) -> int:
    """The day of month that explains the most charges.

    A charge on a month's last day fits any anchor from that day on, since that is what
    clamping produces: a bill anchored on the 31st posts on Feb 28, and a Feb 29 annual bill
    posts on Feb 28 in non-leap years. Ties go to the later day.
    """

    def explained(anchor: int) -> int:
        return sum(
            d.day == anchor or (d.day < anchor and d.day == days_in_month(d.year, d.month))
            for d in dates
        )

    return max({d.day for d in dates}, key=lambda anchor: (explained(anchor), anchor))


def _missed_cycles(cadence: Cadence, next_expected: date, as_of: date) -> int:
    if as_of < next_expected:
        return 0
    return 1 + int((as_of - next_expected).days // NOMINAL_DAYS[cadence])
