import calendar
from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from cashflow.core.calendar import add_months


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 1, 1)), st.integers(-240, 240))
def test_add_months_shifts_month_and_clamps_day(d: date, months: int) -> None:
    result = add_months(d, months)

    assert (result.year * 12 + result.month) - (d.year * 12 + d.month) == months
    assert result.day == min(d.day, calendar.monthrange(result.year, result.month)[1])
