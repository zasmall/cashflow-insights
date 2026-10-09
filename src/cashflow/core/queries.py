"""Read services shared by the API and the MCP server, which stay thin wrappers around them.

Every function is scoped by `entity_id` and raises `NotFoundError` for an unknown entity, and
for ids that belong to a different entity. Results are Pydantic models; money is `Decimal`.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cashflow.core.enums import AnomalyStatus, AnomalyType, Cadence, Severity
from cashflow.core.errors import InvalidRequestError, NotFoundError
from cashflow.core.forecast import BalancePoint
from cashflow.core.recurring import detect_recurring
from cashflow.core.summary import (
    EntityInfo,
    OpenAnomaly,
    StoredForecast,
    WeeklySummary,
    build_summary,
    summary_week,
)
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.settings import Settings

SEVERITY_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def require_entity(session: Session, entity_id: str) -> orm.Entity:
    entity = session.get(orm.Entity, entity_id)
    if entity is None:
        raise NotFoundError("entity not found")
    return entity


# Entities --------------------------------------------------------------------------------------


class EntityOverview(BaseModel):
    id: str
    name: str
    currency: str
    last_transaction_on: date | None
    open_anomalies: int
    refresh_pending: bool


def list_entities(session: Session) -> list[EntityOverview]:
    last_txn = (
        select(orm.Transaction.entity_id, func.max(orm.Transaction.posted_on).label("last_on"))
        .group_by(orm.Transaction.entity_id)
        .subquery()
    )
    open_count = (
        select(orm.Anomaly.entity_id, func.count().label("open"))
        .where(orm.Anomaly.status == AnomalyStatus.OPEN)
        .group_by(orm.Anomaly.entity_id)
        .subquery()
    )
    rows = session.execute(
        select(orm.Entity, last_txn.c.last_on, open_count.c.open)
        .outerjoin(last_txn, last_txn.c.entity_id == orm.Entity.id)
        .outerjoin(open_count, open_count.c.entity_id == orm.Entity.id)
        .order_by(orm.Entity.id)
    )
    return [
        EntityOverview(
            id=entity.id,
            name=entity.name,
            currency=entity.currency,
            last_transaction_on=last_on,
            open_anomalies=open_anomalies or 0,
            refresh_pending=entity.dirty_since is not None,
        )
        for entity, last_on, open_anomalies in rows
    ]


# Forecast --------------------------------------------------------------------------------------


class ForecastPointOut(BaseModel):
    date: date
    expected_balance: Decimal
    lower: Decimal
    upper: Decimal


class BacktestOut(BaseModel):
    mase: float | None
    """Daily net-flow error relative to a same-weekday-last-week guess; below 1 beats it."""
    coverage: float | None
    """Share of backtest days the real balance stayed inside the band."""
    balance_error: Decimal | None
    """Typical gap between forecast and actual balance over this horizon, in currency."""


class ForecastOut(BaseModel):
    entity_id: str
    horizon_days: int
    as_of: date
    generated_at: datetime
    starting_balance: Decimal
    confidence_level: int
    model: str
    backtest: BacktestOut
    points: list[ForecastPointOut]


def get_forecast(session: Session, entity_id: str, horizon: int, settings: Settings) -> ForecastOut:
    if horizon not in settings.forecast.horizons_days:
        allowed = ", ".join(str(h) for h in sorted(settings.forecast.horizons_days))
        raise InvalidRequestError(f"horizon must be one of {allowed}")
    require_entity(session, entity_id)
    forecast = repositories.latest_forecast(session, entity_id, horizon)
    if forecast is None:
        raise NotFoundError("no forecast yet for this entity")

    return ForecastOut(
        entity_id=entity_id,
        horizon_days=forecast.horizon_days,
        as_of=forecast.as_of,
        generated_at=forecast.generated_at,
        starting_balance=forecast.starting_balance,
        confidence_level=settings.forecast.confidence_level,
        model=forecast.model,
        backtest=BacktestOut(
            mase=forecast.backtest_mase,
            coverage=forecast.backtest_coverage,
            balance_error=forecast.backtest_balance_error,
        ),
        points=[
            ForecastPointOut(
                date=p.on_date, expected_balance=p.expected_balance, lower=p.lower, upper=p.upper
            )
            for p in forecast.points
        ],
    )


# Anomalies -------------------------------------------------------------------------------------


class AnomalyOut(BaseModel):
    id: int
    type: AnomalyType
    severity: Severity
    status: AnomalyStatus
    explanation: str
    transaction_ids: list[int]
    detected_at: datetime
    dismissed_at: datetime | None

    @classmethod
    def of(cls, anomaly: orm.Anomaly) -> Self:
        return cls(
            id=anomaly.id,
            type=anomaly.type,
            severity=anomaly.severity,
            status=anomaly.status,
            explanation=anomaly.explanation,
            transaction_ids=anomaly.transaction_ids,
            detected_at=anomaly.detected_at,
            dismissed_at=anomaly.dismissed_at,
        )


class TransactionOut(BaseModel):
    id: int
    source_id: str
    posted_on: date
    amount: Decimal
    """Negative is money out."""
    vendor: str
    category: str
    description: str


class AnomalyDetail(BaseModel):
    anomaly: AnomalyOut
    evidence: list[TransactionOut]
    """The transactions behind the finding. For a missed bill, the last charge that did arrive."""


def list_anomalies(session: Session, entity_id: str, status: AnomalyStatus) -> list[AnomalyOut]:
    """Most severe first, then most recently detected."""
    require_entity(session, entity_id)
    anomalies = repositories.list_anomalies(session, entity_id, status)
    anomalies.sort(key=lambda a: (SEVERITY_RANK[a.severity], -a.detected_at.timestamp(), -a.id))
    return [AnomalyOut.of(a) for a in anomalies]


def get_anomaly(session: Session, entity_id: str, anomaly_id: int) -> orm.Anomaly:
    require_entity(session, entity_id)
    anomaly = repositories.get_anomaly(session, entity_id, anomaly_id)
    if anomaly is None:
        raise NotFoundError("anomaly not found")
    return anomaly


def explain_anomaly(session: Session, entity_id: str, anomaly_id: int) -> AnomalyDetail:
    anomaly = get_anomaly(session, entity_id, anomaly_id)
    evidence = session.scalars(
        select(orm.Transaction)
        .where(
            orm.Transaction.entity_id == entity_id,
            orm.Transaction.id.in_(anomaly.transaction_ids),
        )
        .order_by(orm.Transaction.posted_on, orm.Transaction.id)
    )
    return AnomalyDetail(
        anomaly=AnomalyOut.of(anomaly),
        evidence=[
            TransactionOut(
                id=t.id,
                source_id=t.source_id,
                posted_on=t.posted_on,
                amount=t.amount,
                vendor=t.vendor,
                category=t.category,
                description=t.description,
            )
            for t in evidence
        ],
    )


# Spend by category -----------------------------------------------------------------------------


class CategoryTotal(BaseModel):
    category: str
    money_out: Decimal
    money_in: Decimal
    transactions: int


class SpendByCategory(BaseModel):
    entity_id: str
    start: date
    end: date
    money_out: Decimal
    money_in: Decimal
    categories: list[CategoryTotal]
    """Largest money out first."""


def spend_by_category(session: Session, entity_id: str, start: date, end: date) -> SpendByCategory:
    if start > end:
        raise InvalidRequestError("start must be on or before end")
    require_entity(session, entity_id)
    out_ = func.coalesce(func.sum(-orm.Transaction.amount).filter(orm.Transaction.amount < 0), 0)
    in_ = func.coalesce(func.sum(orm.Transaction.amount).filter(orm.Transaction.amount > 0), 0)
    rows = session.execute(
        select(orm.Transaction.category, out_, in_, func.count())
        .where(
            orm.Transaction.entity_id == entity_id,
            orm.Transaction.posted_on.between(start, end),
        )
        .group_by(orm.Transaction.category)
    ).all()
    categories = sorted(
        (
            CategoryTotal(category=c, money_out=Decimal(o), money_in=Decimal(i), transactions=n)
            for c, o, i, n in rows
        ),
        key=lambda c: (-c.money_out, -c.money_in, c.category),
    )
    return SpendByCategory(
        entity_id=entity_id,
        start=start,
        end=end,
        money_out=sum((c.money_out for c in categories), Decimal(0)),
        money_in=sum((c.money_in for c in categories), Decimal(0)),
        categories=categories,
    )


# Recurring -------------------------------------------------------------------------------------


class RecurringOut(BaseModel):
    vendor: str
    cadence: Cadence
    typical_amount: Decimal
    """Negative is a bill, positive is income."""
    projected_amount: Decimal
    """The amount to expect next: typical, or a new price confirmed by two charges."""
    last_seen_on: date
    next_expected_on: date


def list_recurring(session: Session, entity_id: str) -> list[RecurringOut]:
    """As of the entity's last refresh: the series its forecast used."""
    require_entity(session, entity_id)
    series = session.scalars(
        select(orm.RecurringSeries)
        .where(orm.RecurringSeries.entity_id == entity_id)
        .order_by(orm.RecurringSeries.next_expected_on, orm.RecurringSeries.vendor)
    )
    return [
        RecurringOut(
            vendor=s.vendor,
            cadence=s.cadence,
            typical_amount=s.typical_amount,
            projected_amount=s.projected_amount,
            last_seen_on=s.last_seen_on,
            next_expected_on=s.next_expected_on,
        )
        for s in series
    ]


# Weekly summary --------------------------------------------------------------------------------


def weekly_summary(
    session: Session, entity_id: str, *, today: date, week_of: date | None, settings: Settings
) -> WeeklySummary:
    entity = require_entity(session, entity_id)
    history = repositories.entity_history(session, entity_id)
    if history is None:  # only if the entity vanished since the check above
        raise NotFoundError("entity not found")
    week_start, _ = summary_week(today, week_of)
    if week_start > today:
        raise InvalidRequestError("week_of can't be in the future")

    series = detect_recurring(history.transactions, as_of=today, settings=settings.recurring)
    return build_summary(
        EntityInfo(entity.id, entity.name, entity.currency),
        history,
        series,
        _stored_forecast(session, entity_id, settings),
        [
            OpenAnomaly(a.id, a.type, a.severity, a.explanation, a.detected_at)
            for a in repositories.list_anomalies(session, entity_id, AnomalyStatus.OPEN)
        ],
        today=today,
        week_start=week_start,
        refresh_pending=entity.dirty_since is not None,
    )


def _stored_forecast(session: Session, entity_id: str, settings: Settings) -> StoredForecast | None:
    by_horizon = {
        h: repositories.latest_forecast(session, entity_id, h)
        for h in settings.forecast.horizons_days
    }
    longest = by_horizon[settings.forecast.max_horizon]
    if longest is None:
        return None
    return StoredForecast(
        as_of=longest.as_of,
        model=longest.model,
        points=[
            BalancePoint(p.on_date, p.expected_balance, p.lower, p.upper) for p in longest.points
        ],
        typical_error={h: f.backtest_balance_error for h, f in by_horizon.items() if f is not None},
    )
