"""Recompute an entity's derived data from its transactions.

M4 and M5 add the forecast and the anomaly scan here, so one call refreshes everything
after new transactions arrive.
"""

from datetime import date

from sqlalchemy.orm import Session

from cashflow.core.recurring import DetectedSeries, detect_recurring
from cashflow.db import repositories
from cashflow.settings import RecurringSettings


def refresh_recurring(
    session: Session, entity_id: str, *, as_of: date, settings: RecurringSettings
) -> list[DetectedSeries]:
    """Detect the entity's recurring series and replace the stored ones. The caller commits."""
    transactions = repositories.entity_transactions(session, entity_id)
    series = detect_recurring(transactions, as_of=as_of, settings=settings)
    repositories.replace_recurring_series(session, entity_id, series)
    return series
