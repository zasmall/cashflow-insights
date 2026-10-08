from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cashflow.core.enums import AnomalyType, Cadence, Severity
from cashflow.core.forecast import BalancePoint, History
from cashflow.core.models import Transaction
from cashflow.core.recurring import DetectedSeries
from cashflow.core.summary import (
    EntityInfo,
    OpenAnomaly,
    StoredForecast,
    WeeklySummary,
    build_summary,
    summary_week,
)

TODAY = date(2026, 10, 8)  # a Thursday
WEEK_START = date(2026, 9, 28)
ENTITY = EntityInfo("e", "Test Co", "USD")


def txn(
    posted_on: date, amount: str, vendor: str = "Shop", category: str = "Supplies", n: int = 0
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


def summarize(
    transactions: list[Transaction],
    series: list[DetectedSeries] | None = None,
    forecast: StoredForecast | None = None,
    anomalies: list[OpenAnomaly] | None = None,
) -> WeeklySummary:
    history = History(transactions, Decimal("1000.00"), date(2026, 1, 1))
    return build_summary(
        ENTITY,
        history,
        series or [],
        forecast,
        anomalies or [],
        today=TODAY,
        week_start=WEEK_START,
        refresh_pending=False,
    )


# Which week ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "start"),
    [
        (date(2026, 10, 5), date(2026, 9, 28)),  # Monday: the week that just ended
        (date(2026, 10, 8), date(2026, 9, 28)),  # Thursday
        (date(2026, 10, 11), date(2026, 9, 28)),  # Sunday: this week isn't over yet
    ],
)
def test_default_is_the_last_complete_monday_to_sunday_week(today: date, start: date) -> None:
    assert summary_week(today) == (start, start + timedelta(days=6))


@pytest.mark.parametrize("week_of", [date(2026, 9, 14), date(2026, 9, 16), date(2026, 9, 20)])
def test_week_of_picks_the_week_containing_it(week_of: date) -> None:
    assert summary_week(TODAY, week_of) == (date(2026, 9, 14), date(2026, 9, 20))


# Cash ------------------------------------------------------------------------------------------


def test_week_cash_totals_and_comparisons() -> None:
    transactions = [
        txn(date(2026, 9, 1), "-100.00"),  # before every window, but in the balance
        txn(date(2026, 9, 21), "500.00", "Client"),  # previous week
        txn(date(2026, 9, 22), "-200.00"),
        txn(date(2026, 9, 28), "3000.00", "Client"),  # summary week
        txn(date(2026, 10, 4), "-1250.50"),
        txn(date(2026, 10, 5), "-999.00"),  # after the week
    ]

    cash = summarize(transactions).cash

    assert cash.start_balance == Decimal("1200.00")
    assert cash.end_balance == Decimal("2949.50")
    assert (cash.this_week.money_in, cash.this_week.money_out) == (
        Decimal("3000.00"),
        Decimal("1250.50"),
    )
    assert cash.this_week.net == Decimal("1749.50")
    assert cash.previous_week.net == Decimal("300.00")
    assert cash.four_week_average.money_in == Decimal("125.00")  # 500 over 4 weeks
    assert cash.four_week_average.money_out == Decimal("75.00")  # 100 + 200 over 4 weeks


@given(
    st.lists(
        st.tuples(
            st.dates(min_value=date(2026, 8, 1), max_value=TODAY),
            st.decimals(min_value=-5000, max_value=5000, places=2).filter(lambda d: d != 0),
        ),
        max_size=60,
    )
)
def test_week_cash_always_balances(rows: list[tuple[date, Decimal]]) -> None:
    cash = summarize([txn(d, str(a), n=k) for k, (d, a) in enumerate(rows)]).cash

    assert cash.this_week.net == cash.this_week.money_in - cash.this_week.money_out
    assert cash.end_balance == cash.start_balance + cash.this_week.net


# Discretionary categories ----------------------------------------------------------------------


def test_top_discretionary_categories() -> None:
    rent = DetectedSeries(
        "Landlord", Cadence.MONTHLY, Decimal("-3000.00"), 9, date(2026, 10, 1), date(2026, 11, 1), 1
    )
    transactions = [
        txn(date(2026, 10, 1), "-3000.00", "Landlord", "Rent"),  # recurring: excluded
        txn(date(2026, 9, 29), "-80.00", "Cafe", "Meals"),
        txn(date(2026, 9, 30), "-80.00", "Shell", "Fuel"),
        txn(date(2026, 10, 2), "-400.00", "Staples", "Supplies"),
        txn(date(2026, 9, 15), "-100.00", "Staples", "Supplies"),  # a comparison week
        *(txn(date(2026, 10, 3), "-1.00", "Misc", f"Cat{k}") for k in range(5)),
    ]

    top = summarize(transactions, [rent]).top_discretionary_categories

    assert [c.category for c in top] == ["Supplies", "Fuel", "Meals", "Cat0", "Cat1"]
    assert top[0].spent == Decimal("400.00")
    assert top[0].four_week_average == Decimal("25.00")


# Upcoming --------------------------------------------------------------------------------------


def test_upcoming_lists_the_next_seven_days_in_date_order() -> None:
    def monthly(vendor: str, amount: str, next_on: date) -> DetectedSeries:
        return DetectedSeries(
            vendor,
            Cadence.MONTHLY,
            Decimal(amount),
            6,
            next_on - timedelta(30),
            next_on,
            next_on.day,
        )

    series = [
        monthly("Figma", "-45.00", date(2026, 10, 15)),  # day 7: included
        monthly("Rent", "-3000.00", date(2026, 10, 9)),
        monthly("Initech", "4000.00", date(2026, 10, 12)),
        monthly("Slack", "-43.75", date(2026, 10, 16)),  # day 8: not yet
        monthly("Google", "-72.00", date(2026, 9, 3)),  # overdue: an anomaly, not a plan
    ]

    upcoming = summarize([], series).upcoming

    assert [(u.on_date, u.vendor, u.amount) for u in upcoming] == [
        (date(2026, 10, 9), "Rent", Decimal("-3000.00")),
        (date(2026, 10, 12), "Initech", Decimal("4000.00")),
        (date(2026, 10, 15), "Figma", Decimal("-45.00")),
    ]


# Outlook ---------------------------------------------------------------------------------------


def test_outlook_reports_horizons_and_low_points() -> None:
    expected = [Decimal(1000 - 10 * d if d <= 40 else 600 + 5 * (d - 40)) for d in range(1, 91)]
    points = [
        BalancePoint(TODAY + timedelta(days=d), e, e - 20 * d, e + 20 * d)
        for d, e in enumerate(expected, start=1)
    ]
    forecast = StoredForecast(
        TODAY, "AutoETS", points, {30: Decimal("50"), 60: None, 90: Decimal("90")}
    )

    outlook = summarize([], forecast=forecast).outlook

    assert outlook is not None
    assert [(h.horizon_days, h.expected, h.typical_error) for h in outlook.horizons] == [
        (30, Decimal(700), Decimal("50")),
        (60, Decimal(700), None),
        (90, Decimal(850), Decimal("90")),
    ]
    assert outlook.lowest_expected.on_date == TODAY + timedelta(days=40)
    assert outlook.lowest_expected.balance == Decimal(600)
    assert outlook.lowest_lower_band.balance == min(p.lower for p in points)


def test_no_forecast_yet_means_no_outlook() -> None:
    assert summarize([]).outlook is None


# Anomalies -------------------------------------------------------------------------------------


def test_anomaly_digest_ranks_counts_and_marks_new() -> None:
    def anomaly(n: int, severity: Severity, detected: date) -> OpenAnomaly:
        return OpenAnomaly(
            n,
            AnomalyType.DUPLICATE_CHARGE,
            severity,
            f"#{n}",
            datetime(detected.year, detected.month, detected.day, tzinfo=UTC),
        )

    anomalies = [
        anomaly(1, Severity.LOW, date(2026, 9, 1)),
        anomaly(2, Severity.HIGH, date(2026, 9, 2)),
        anomaly(3, Severity.HIGH, date(2026, 10, 1)),
        anomaly(4, Severity.MEDIUM, date(2026, 9, 28)),
        *(anomaly(n, Severity.LOW, date(2026, 8, 1)) for n in range(5, 9)),
    ]

    digest = summarize([], anomalies=anomalies).anomalies

    assert list(digest.open_by_severity.items()) == [
        (Severity.HIGH, 2),
        (Severity.MEDIUM, 1),
        (Severity.LOW, 5),
    ]
    assert [a.id for a in digest.top] == [3, 2, 4, 1, 8]
    assert digest.new_count == 2
    assert {a.id for a in digest.top if a.new} == {3, 4}
