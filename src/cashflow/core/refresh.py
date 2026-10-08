"""Recompute an entity's derived data from its transactions: recurring series, the forecast,
and the anomaly scan. One call refreshes everything after new transactions arrive."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from cashflow.core.anomalies import scan
from cashflow.core.forecast import ForecastRun, build_forecast
from cashflow.core.recurring import DetectedSeries, detect_recurring
from cashflow.db import repositories
from cashflow.settings import RecurringSettings, Settings


class UnknownEntityError(LookupError):
    pass


@dataclass(frozen=True)
class RefreshResult:
    entity_id: str
    series: list[DetectedSeries]
    forecast: ForecastRun
    anomalies: repositories.AnomalySyncResult


def refresh_recurring(
    session: Session, entity_id: str, *, as_of: date, settings: RecurringSettings
) -> list[DetectedSeries]:
    """Detect the entity's recurring series and replace the stored ones. The caller commits."""
    transactions = repositories.entity_transactions(session, entity_id)
    series = detect_recurring(transactions, as_of=as_of, settings=settings)
    repositories.replace_recurring_series(session, entity_id, series)
    return series


def refresh_entity(
    session: Session, entity_id: str, *, as_of: date, settings: Settings
) -> RefreshResult:
    """Recompute and store everything derived for one entity. The caller commits."""
    history = repositories.entity_history(session, entity_id)
    if history is None:
        raise UnknownEntityError(entity_id)
    series = refresh_recurring(session, entity_id, as_of=as_of, settings=settings.recurring)
    run = build_forecast(
        history, as_of=as_of, recurring=settings.recurring, forecast=settings.forecast
    )
    repositories.save_forecast(session, entity_id, run)
    findings = scan(history.transactions, series, as_of=as_of, settings=settings.anomaly)
    anomalies = repositories.sync_anomalies(session, entity_id, findings)
    return RefreshResult(entity_id, series, run, anomalies)


def refresh_if_dirty(
    factory: sessionmaker[Session], entity_id: str, *, as_of: date, settings: Settings
) -> RefreshResult | None:
    """Claim and refresh a dirty entity in one transaction.

    If the refresh fails, the claim rolls back with it, so the entity stays dirty and a later
    run (another event, or `python -m cashflow.refresh --dirty`) retries it.
    """
    with factory() as session:
        if not repositories.claim_dirty(session, entity_id):
            return None
        result = refresh_entity(session, entity_id, as_of=as_of, settings=settings)
        session.commit()
        return result
