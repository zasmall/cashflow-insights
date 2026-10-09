from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cashflow.core.anomalies import Finding, scan
from cashflow.core.calendar import add_months
from cashflow.core.enums import AnomalyType, Cadence, Severity
from cashflow.core.models import Transaction
from cashflow.core.recurring import DetectedSeries, detect_recurring
from cashflow.demo.generator import generate
from cashflow.settings import AnomalySettings, RecurringSettings

SETTINGS = AnomalySettings()
AS_OF = date(2026, 10, 8)
HISTORY_START = date(2025, 1, 1)


def txn(
    posted_on: date, amount: str, vendor: str = "Shell", category: str = "Fuel", n: int = 0
) -> Transaction:
    return Transaction(
        entity_id="e",
        source_id=f"{vendor}-{posted_on}-{amount}-{n}",
        account_id="a",
        posted_on=posted_on,
        amount=Decimal(amount),
        description="",
        vendor=vendor,
        category=category,
    )


ANCHOR = txn(HISTORY_START, "-5.00", "Corner Deli", "Meals")
"""Starts the history early, so warm-up and baselines are satisfied unless a test says otherwise."""


def found(
    transactions: list[Transaction],
    series: list[DetectedSeries] | None = None,
    as_of: date = AS_OF,
    **overrides: object,
) -> list[Finding]:
    anomaly_settings = SETTINGS.model_copy(update=overrides)
    return scan([ANCHOR, *transactions], series or [], as_of=as_of, settings=anomaly_settings)


def of_type(findings: list[Finding], kind: AnomalyType) -> list[Finding]:
    return [f for f in findings if f.type is kind]


# duplicate_charge ------------------------------------------------------------------------------


def test_same_vendor_and_amount_within_window_is_a_duplicate() -> None:
    pair = [txn(date(2026, 9, 20), "-64.20"), txn(date(2026, 9, 22), "-64.20")]

    (dup,) = of_type(found(pair), AnomalyType.DUPLICATE_CHARGE)

    assert dup.subject == "Shell"
    assert dup.source_ids == tuple(sorted(t.source_id for t in pair))
    assert dup.severity is Severity.LOW
    assert "$64.20 2 times" in dup.explanation


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(txn(date(2026, 9, 24), "-64.20"), id="outside-window"),
        pytest.param(txn(date(2026, 9, 21), "-64.21"), id="different-amount"),
        pytest.param(txn(date(2026, 9, 21), "-64.20", vendor="Chevron"), id="different-vendor"),
    ],
)
def test_near_misses_are_not_duplicates(second: Transaction) -> None:
    first = txn(date(2026, 9, 20), "-64.20")

    assert of_type(found([first, second]), AnomalyType.DUPLICATE_CHARGE) == []


def test_small_and_incoming_repeats_are_not_duplicates() -> None:
    coffees = [
        txn(date(2026, 9, 20), "-4.50", "Blue Bottle"),
        txn(date(2026, 9, 21), "-4.50", "Blue Bottle"),
    ]
    refunds = [txn(date(2026, 9, 20), "64.20"), txn(date(2026, 9, 21), "64.20")]

    assert of_type(found(coffees + refunds), AnomalyType.DUPLICATE_CHARGE) == []


def test_old_duplicates_outside_lookback_are_not_reported() -> None:
    pair = [txn(date(2026, 3, 1), "-64.20"), txn(date(2026, 3, 2), "-64.20")]

    assert of_type(found(pair), AnomalyType.DUPLICATE_CHARGE) == []


def test_a_third_copy_extends_the_same_finding() -> None:
    pair = [txn(date(2026, 9, 20), "-640.00"), txn(date(2026, 9, 21), "-640.00")]
    (before,) = of_type(found(pair), AnomalyType.DUPLICATE_CHARGE)

    (after,) = of_type(
        found([*pair, txn(date(2026, 9, 23), "-640.00")]), AnomalyType.DUPLICATE_CHARGE
    )

    assert after.fingerprint == before.fingerprint
    assert len(after.source_ids) == 3
    assert after.severity is Severity.MEDIUM  # two extra $640 charges: $1,280 impact


# category_spike --------------------------------------------------------------------------------


def monthly_spend(category: str, amounts: list[str], last_month: date) -> list[Transaction]:
    """One charge per month, oldest first, ending in `last_month`."""
    first = add_months(last_month, -(len(amounts) - 1))
    return [
        txn(add_months(first, k).replace(day=5), f"-{amount}", f"Store{k}", category, k)
        for k, amount in enumerate(amounts)
    ]


SEPTEMBER = date(2026, 9, 1)
BASELINE = ["310.00", "280.00", "305.00", "290.00", "330.00", "300.00"]


def test_month_far_above_baseline_is_a_spike() -> None:
    spend = monthly_spend("Office Supplies", [*BASELINE, "2400.00"], SEPTEMBER)

    (spike,) = of_type(found(spend), AnomalyType.CATEGORY_SPIKE)

    assert spike.subject == "Office Supplies"
    assert spike.source_ids == (spend[-1].source_id,)
    assert "in September 2026 was $2,400.00, against a typical $302.50" in spike.explanation
    assert spike.severity is Severity.MEDIUM  # $2,097.50 above typical


def test_month_in_progress_can_already_be_a_spike() -> None:
    spend = monthly_spend("Office Supplies", [*BASELINE, "300.00", "1900.00"], date(2026, 10, 1))

    (spike,) = of_type(found(spend), AnomalyType.CATEGORY_SPIKE)

    assert "so far in October 2026" in spike.explanation


def test_immaterial_rise_is_not_a_spike_however_unusual() -> None:
    steady = ["300.00"] * 6  # MAD 0: only the floor keeps this from being hair-trigger

    assert (
        of_type(
            found(monthly_spend("Fuel", [*steady, "790.00"], SEPTEMBER)), AnomalyType.CATEGORY_SPIKE
        )
        == []
    )
    assert (
        of_type(
            found(monthly_spend("Fuel", [*steady, "810.00"], SEPTEMBER)), AnomalyType.CATEGORY_SPIKE
        )
        != []
    )


def test_sparse_categories_have_no_baseline() -> None:
    travel = ["0", "450.00", "0", "0", "380.00", "500.00"]  # three active months of six
    spend = [t for t in monthly_spend("Travel", [*travel, "3000.00"], SEPTEMBER) if t.amount]

    assert of_type(found(spend), AnomalyType.CATEGORY_SPIKE) == []


def test_recurring_charges_do_not_count_toward_a_spike() -> None:
    spend = monthly_spend("Software", [*BASELINE, "300.00"], SEPTEMBER)
    annual = txn(date(2026, 9, 22), "-2400.00", "Squarespace", "Software")
    series = DetectedSeries(
        "Squarespace",
        Cadence.ANNUAL,
        Decimal("-2400.00"),
        2,
        annual.posted_on,
        date(2027, 9, 22),
        22,
    )

    assert of_type(found([*spend, annual], [series]), AnomalyType.CATEGORY_SPIKE) == []


def test_no_spike_without_enough_history_for_a_baseline() -> None:
    spend = monthly_spend("Office Supplies", [*BASELINE[-3:], "2400.00"], SEPTEMBER)

    assert (
        of_type(scan(spend, [], as_of=AS_OF, settings=SETTINGS), AnomalyType.CATEGORY_SPIKE) == []
    )


# new_vendor_large ------------------------------------------------------------------------------


def test_large_first_charge_from_a_vendor_is_flagged() -> None:
    chair = txn(date(2026, 10, 2), "-4850.00", "Herman Miller", "Furniture")

    (new,) = of_type(found([chair]), AnomalyType.NEW_VENDOR_LARGE)

    assert new.source_ids == (chair.source_id,)
    assert new.severity is Severity.HIGH
    assert "First-ever charge from Herman Miller: $4,850.00 on Oct 2, 2026" in new.explanation


@pytest.mark.parametrize(
    "transactions",
    [
        pytest.param([txn(date(2026, 10, 2), "-999.99", "Dell")], id="below-threshold"),
        pytest.param(
            [txn(date(2025, 6, 1), "-20.00", "Dell"), txn(date(2026, 10, 2), "-1500.00", "Dell")],
            id="known-vendor",
        ),
        pytest.param([txn(date(2026, 10, 2), "5000.00", "Acme")], id="income"),
        pytest.param([txn(date(2026, 5, 2), "-1500.00", "Dell")], id="outside-lookback"),
    ],
)
def test_not_a_large_new_vendor(transactions: list[Transaction]) -> None:
    assert of_type(found(transactions), AnomalyType.NEW_VENDOR_LARGE) == []


def test_every_vendor_is_new_during_warm_up() -> None:
    payroll = txn(date(2026, 9, 11), "-9000.00", "Gusto")

    findings = scan([payroll], [], as_of=AS_OF, settings=SETTINGS)  # history starts with it

    assert of_type(findings, AnomalyType.NEW_VENDOR_LARGE) == []


# missed_recurring ------------------------------------------------------------------------------


def monthly_series(
    vendor: str, amount: str, last_seen: date
) -> tuple[list[Transaction], DetectedSeries]:
    charges = [txn(add_months(last_seen, -k), amount, vendor, "Software", k) for k in range(6)]
    series = DetectedSeries(
        vendor,
        Cadence.MONTHLY,
        Decimal(amount),
        6,
        last_seen,
        add_months(last_seen, 1),
        last_seen.day,
    )
    return charges, series


def test_bill_past_due_plus_grace_is_missed() -> None:
    charges, series = monthly_series("Google Workspace", "-72.00", date(2026, 8, 3))

    (missed,) = of_type(found(charges, [series]), AnomalyType.MISSED_RECURRING)

    assert missed.source_ids == (charges[0].source_id,)  # the last charge that did arrive
    assert (
        "expected on Sep 3, 2026 and hasn't arrived (2 expected charges missed"
        in missed.explanation
    )


def test_bill_within_grace_is_not_yet_missed() -> None:
    charges, series = monthly_series("Slack", "-43.75", date(2026, 9, 3))  # due Oct 3

    assert (
        of_type(found(charges, [series], as_of=date(2026, 10, 8)), AnomalyType.MISSED_RECURRING)
        == []
    )
    assert (
        of_type(found(charges, [series], as_of=date(2026, 10, 9)), AnomalyType.MISSED_RECURRING)
        != []
    )


def test_missed_income_is_reported_as_a_payment() -> None:
    charges, series = monthly_series("Initech", "3500.00", date(2026, 8, 1))

    (missed,) = of_type(found(charges, [series]), AnomalyType.MISSED_RECURRING)

    assert "monthly payment of $3,500.00" in missed.explanation
    assert missed.severity is Severity.HIGH  # two missed $3,500 payments


# recurring_amount_change -----------------------------------------------------------------------


def price_change(new_charges: int) -> tuple[list[Transaction], DetectedSeries]:
    old = [txn(date(2026, m, 12), "-89.99", "Adobe", "Software", m) for m in range(1, 9)]
    new = [
        txn(date(2026, 9 + k, 12), "-104.99", "Adobe", "Software", 9 + k)
        for k in range(new_charges)
    ]
    last = new[-1].posted_on if new else old[-1].posted_on
    series = DetectedSeries(
        "Adobe", Cadence.MONTHLY, Decimal("-89.99"), 8 + new_charges, last, add_months(last, 1), 12
    )
    return [*old, *new], series


def test_recent_charges_off_typical_are_an_amount_change() -> None:
    charges, series = price_change(1)

    (change,) = of_type(found(charges, [series]), AnomalyType.RECURRING_AMOUNT_CHANGE)

    assert change.source_ids == (charges[-1].source_id,)
    assert (
        "rose from $89.99 to $104.99 (16.7%) starting Sep 12, 2026, about $180.00 a year"
        in change.explanation
    )


def test_more_charges_at_the_new_price_extend_the_same_finding() -> None:
    one, series_one = price_change(1)
    two, series_two = price_change(2)

    (before,) = of_type(
        found(one, [series_one], as_of=date(2026, 9, 20)), AnomalyType.RECURRING_AMOUNT_CHANGE
    )
    (after,) = of_type(
        found(two, [series_two], as_of=date(2026, 10, 20)), AnomalyType.RECURRING_AMOUNT_CHANGE
    )

    assert after.fingerprint == before.fingerprint
    assert len(after.source_ids) == 2


def test_change_within_tolerance_or_reverted_is_not_reported() -> None:
    charges, series = price_change(0)
    small = txn(date(2026, 9, 12), "-99.99", "Adobe", "Software", 99)  # +11%, under 15%
    reverted = [
        txn(date(2026, 9, 12), "-104.99", "Adobe", "Software", 98),
        txn(date(2026, 10, 1), "-89.99", "Adobe", "Software", 97),
    ]

    assert of_type(found([*charges, small], [series]), AnomalyType.RECURRING_AMOUNT_CHANGE) == []
    assert (
        of_type(found([*charges, *reverted], [series]), AnomalyType.RECURRING_AMOUNT_CHANGE) == []
    )


# Severity and the whole scan -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "severity"),
    [("-1000.00", Severity.MEDIUM), ("-2499.99", Severity.MEDIUM), ("-2500.00", Severity.HIGH)],
)
def test_severity_follows_dollar_impact(amount: str, severity: Severity) -> None:
    (new,) = of_type(found([txn(date(2026, 10, 1), amount, "Dell")]), AnomalyType.NEW_VENDOR_LARGE)

    assert new.severity is severity


def test_scan_of_nothing_finds_nothing() -> None:
    assert scan([], [], as_of=AS_OF, settings=SETTINGS) == []


@settings(max_examples=40, deadline=None)
@given(
    st.integers(min_value=0, max_value=2**32),
    st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)),
    st.integers(min_value=12, max_value=36),
)
def test_generator_ground_truth_is_found_exactly(seed: int, as_of: date, months: int) -> None:
    """Every planted anomaly is found, with its evidence, and nothing else is."""
    ds = generate(seed, as_of, months)
    series = detect_recurring(ds.transactions, as_of=as_of, settings=RecurringSettings())

    findings = scan(ds.transactions, series, as_of=as_of, settings=SETTINGS)

    assert sorted((f.type, f.subject) for f in findings) == sorted(
        (a.type, a.subject) for a in ds.anomalies
    )
    for planted in ds.anomalies:
        (match,) = [f for f in findings if f.type is planted.type]
        assert set(planted.source_ids) <= set(match.source_ids)


def test_missed_bill_expects_a_confirmed_new_price() -> None:
    charges, series = monthly_series("Adobe", "-89.99", date(2026, 8, 12))
    repriced = DetectedSeries(
        series.vendor,
        series.cadence,
        series.typical_amount,
        series.occurrences,
        series.last_seen_on,
        series.next_expected_on,
        series.anchor_day,
        changed_amount=Decimal("-104.99"),
    )

    (missed,) = of_type(found(charges, [repriced]), AnomalyType.MISSED_RECURRING)

    assert "monthly charge of $104.99 was expected" in missed.explanation
