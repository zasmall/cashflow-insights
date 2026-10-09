from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cashflow.core.calendar import add_months
from cashflow.core.enums import Cadence
from cashflow.core.models import Transaction
from cashflow.core.recurring import DetectedSeries, detect_recurring
from cashflow.demo.generator import generate
from cashflow.settings import RecurringSettings

SETTINGS = RecurringSettings()
STEP_DAYS = {Cadence.WEEKLY: 7, Cadence.BIWEEKLY: 14}
STEP_MONTHS = {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.ANNUAL: 12}


def txn(posted_on: date, amount: str | Decimal, vendor: str = "Adobe", n: int = 0) -> Transaction:
    return Transaction(
        entity_id="e",
        source_id=f"{vendor}-{posted_on}-{n}",
        account_id="a",
        posted_on=posted_on,
        amount=Decimal(amount),
        description="",
        vendor=vendor,
        category="Software",
    )


def nth(cadence: Cadence, start: date, k: int) -> date:
    """The k-th scheduled date, computed independently of the detector's stepping."""
    if cadence in STEP_DAYS:
        return start + timedelta(days=STEP_DAYS[cadence] * k)
    return add_months(start, STEP_MONTHS[cadence] * k)


def series(
    cadence: Cadence, start: date, count: int, amount: str = "-89.99", vendor: str = "Adobe"
) -> list[Transaction]:
    return [txn(nth(cadence, start, k), amount, vendor) for k in range(count)]


def detect(transactions: list[Transaction], as_of: date | None = None) -> list[DetectedSeries]:
    as_of = as_of or max(t.posted_on for t in transactions)
    return detect_recurring(transactions, as_of=as_of, settings=SETTINGS)


# Examples --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cadence", "count", "next_expected"),
    [
        (Cadence.WEEKLY, 6, date(2026, 2, 26)),
        (Cadence.BIWEEKLY, 6, date(2026, 4, 9)),
        (Cadence.MONTHLY, 6, date(2026, 7, 15)),
        (Cadence.QUARTERLY, 4, date(2027, 1, 15)),
        (Cadence.ANNUAL, 2, date(2028, 1, 15)),
    ],
)
def test_each_cadence_is_detected(cadence: Cadence, count: int, next_expected: date) -> None:
    (found,) = detect(series(cadence, date(2026, 1, 15), count))

    assert found.cadence is cadence
    assert found.typical_amount == Decimal("-89.99")
    assert found.occurrences == count
    assert found.next_expected_on == next_expected


def test_month_end_bills_keep_their_anchor_day() -> None:
    (found,) = detect(series(Cadence.MONTHLY, date(2026, 1, 31), 6))  # ... Apr 30, May 31, Jun 30

    assert found.last_seen_on == date(2026, 6, 30)
    assert found.next_expected_on == date(2026, 7, 31)


def test_leap_day_annual_bill_returns_to_the_29th() -> None:
    (found,) = detect(series(Cadence.ANNUAL, date(2016, 2, 29), 4))  # 2017-2019 post on Feb 28

    assert found.next_expected_on == date(2020, 2, 29)


def test_recent_price_increase_keeps_the_series_and_its_old_typical_amount() -> None:
    charges = [
        *series(Cadence.MONTHLY, date(2025, 1, 12), 10),
        txn(date(2025, 11, 12), "-104.99"),
        txn(date(2025, 12, 12), "-104.99"),
    ]

    (found,) = detect(charges)

    assert found.typical_amount == Decimal("-89.99")
    assert found.last_seen_on == date(2025, 12, 12)
    assert found.projected_amount == Decimal("-104.99"), "two charges confirm the new price"


def price_history(*new_prices: str) -> list[Transaction]:
    old = series(Cadence.MONTHLY, date(2025, 1, 12), 10)
    return [*old, *(txn(date(2025, 11 + k, 12), p, n=k) for k, p in enumerate(new_prices))]


@pytest.mark.parametrize(
    "new_prices",
    [
        pytest.param(("-104.99",), id="one-charge-is-not-enough"),
        pytest.param(("-104.99", "-129.99"), id="new-charges-disagree"),
        pytest.param(("-104.99", "-89.99"), id="reverted"),
        pytest.param(("-95.00", "-95.00"), id="within-tolerance"),
    ],
)
def test_unconfirmed_changes_keep_projecting_the_typical_amount(
    new_prices: tuple[str, ...],
) -> None:
    (found,) = detect(price_history(*new_prices))

    assert found.changed_amount is None
    assert found.projected_amount == found.typical_amount == Decimal("-89.99")


def test_one_skipped_charge_does_not_break_a_series() -> None:
    charges = [t for k, t in enumerate(series(Cadence.MONTHLY, date(2025, 1, 5), 12)) if k != 6]

    (found,) = detect(charges)

    assert found.cadence is Cadence.MONTHLY


def test_refunds_are_a_separate_direction() -> None:
    charges = [*series(Cadence.MONTHLY, date(2025, 1, 5), 6), txn(date(2025, 3, 9), "89.99")]

    (found,) = detect(charges)

    assert found.typical_amount == Decimal("-89.99")


@pytest.mark.parametrize(
    ("cadence", "count"),
    [(Cadence.WEEKLY, 3), (Cadence.MONTHLY, 2), (Cadence.QUARTERLY, 2), (Cadence.ANNUAL, 1)],
)
def test_too_few_charges_are_not_a_series(cadence: Cadence, count: int) -> None:
    assert detect(series(cadence, date(2026, 1, 15), count)) == []


def test_two_annual_charges_must_match_exactly() -> None:
    charges = [
        txn(date(2025, 3, 2), "-289.36", "United"),
        txn(date(2026, 3, 2), "-301.10", "United"),
    ]

    assert detect(charges) == []


def test_series_that_stopped_long_ago_has_ended() -> None:
    charges = series(Cadence.MONTHLY, date(2025, 1, 3), 6)  # last charge 2025-06-03

    assert detect(charges, as_of=date(2025, 8, 20)) != []  # missed 2: still active, overdue
    assert detect(charges, as_of=date(2025, 11, 20)) == []  # missed 5: cancelled


def test_two_subscriptions_from_one_vendor_are_not_detected() -> None:
    """Documented limitation: the amounts split, so neither reaches the stable share."""
    charges = series(Cadence.MONTHLY, date(2025, 1, 5), 8, "-89.99") + series(
        Cadence.MONTHLY, date(2025, 1, 20), 8, "-22.99"
    )

    assert detect(charges) == []


def test_no_transactions() -> None:
    assert detect_recurring([], as_of=date(2026, 1, 1), settings=SETTINGS) == []


# Properties ------------------------------------------------------------------------------------

cadences = st.sampled_from(Cadence)
starts = st.dates(min_value=date(2010, 1, 1), max_value=date(2035, 12, 31))
base_cents = st.integers(min_value=500, max_value=1_000_000)


@settings(max_examples=200)
@given(cadences, starts, base_cents, st.booleans(), st.data())
def test_regular_series_is_recovered(
    cadence: Cadence, start: date, cents: int, income: bool, data: st.DataObject
) -> None:
    minimum = SETTINGS.min_occurrences[cadence]
    count = data.draw(st.integers(max(minimum, 3), minimum + 15), label="count")
    start = start.replace(day=data.draw(st.integers(2, 27), label="anchor day"))
    sign = 1 if income else -1
    base = Decimal(sign * cents) / 100
    charges = []
    for k in range(count):
        jitter = timedelta(days=data.draw(st.integers(-1, 1)))
        noise = Decimal(data.draw(st.integers(-40, 40))) / 1000  # within +/-4%
        charges.append(
            txn(
                nth(cadence, start, k) + jitter, (base * (1 + noise)).quantize(Decimal("0.01")), n=k
            )
        )

    (found,) = detect(charges, as_of=nth(cadence, start, count - 1))

    assert found.cadence is cadence
    assert abs(found.typical_amount - base) <= abs(base) * SETTINGS.amount_tolerance
    assert abs((found.next_expected_on - nth(cadence, start, count)).days) <= 1
    assert found.changed_amount is None, "noise within tolerance isn't a price change"


@given(cadences, starts, st.integers(2, 12))
def test_exact_schedules_on_any_day_are_recovered(
    cadence: Cadence, start: date, extra: int
) -> None:
    count = SETTINGS.min_occurrences[cadence] + extra

    (found,) = detect(series(cadence, start, count))

    assert found.cadence is cadence
    assert found.next_expected_on == nth(cadence, start, count)


@given(cadences, starts, st.integers(3, 20), st.permutations(range(20)))
def test_unstable_amounts_are_never_a_series(
    cadence: Cadence, start: date, count: int, order: list[int]
) -> None:
    # Each amount is 25% away from the next, so at most one sits within 10% of the median.
    amounts = [Decimal(-100) * Decimal("1.25") ** k for k in order if k < count]
    charges = [
        txn(nth(cadence, start, k), amount.quantize(Decimal("0.01")), n=k)
        for k, amount in enumerate(amounts)
    ]

    assert detect(charges) == []


IRREGULAR_GAPS = st.one_of(st.integers(19, 23), st.integers(40, 80), st.integers(120, 300))


@given(starts, st.lists(IRREGULAR_GAPS, min_size=3, max_size=20))
def test_irregular_timing_is_never_a_series(start: date, gaps: list[int]) -> None:
    # No gap is within tolerance of any cadence's step, whatever the amounts.
    dates = [start]
    for gap in gaps:
        dates.append(dates[-1] + timedelta(days=gap))

    assert detect([txn(d, "-50.00", n=k) for k, d in enumerate(dates)]) == []


@settings(max_examples=60, deadline=None)
@given(
    st.integers(min_value=0, max_value=2**32),
    st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)),
    st.integers(min_value=12, max_value=36),
)
def test_generator_ground_truth_is_recovered_exactly(seed: int, as_of: date, months: int) -> None:
    """Planted series are found and nothing else is: not AWS, meals, fuel, or client revenue.

    From 12 months of history, so two raised Adobe charges stay a minority of the series.
    """
    ds = generate(seed, as_of, months)
    expected = {
        (s.vendor, s.cadence, s.typical_amount, s.projected_amount)
        for s in ds.recurring
        if s.occurrences >= SETTINGS.min_occurrences[s.cadence]
    }

    found = detect_recurring(ds.transactions, as_of=as_of, settings=SETTINGS)

    assert {(s.vendor, s.cadence, s.typical_amount, s.projected_amount) for s in found} == expected
