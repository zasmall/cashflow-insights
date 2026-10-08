"""Calendar arithmetic shared by the demo generator, recurring detection, and forecasting."""

import calendar
from datetime import date


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(d: date, months: int) -> date:
    """Shift by whole months, clamping the day to the target month's length."""
    month_index = d.year * 12 + d.month - 1 + months
    year, month0 = divmod(month_index, 12)
    return date(year, month0 + 1, min(d.day, days_in_month(year, month0 + 1)))
