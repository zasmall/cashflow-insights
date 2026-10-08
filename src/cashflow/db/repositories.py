"""Persistence operations. Callers own the transaction: nothing here commits."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import batched

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from cashflow.core.anomalies import Finding
from cashflow.core.enums import AnomalyStatus, AnomalyType, InboundEventStatus
from cashflow.core.events import RelayEnvelope
from cashflow.core.forecast import ForecastRun, History
from cashflow.core.models import Entity, Transaction
from cashflow.core.recurring import DetectedSeries
from cashflow.db import models as orm

# Postgres caps a statement at 65,535 bind parameters; 10 columns x 1,000 rows stays well under.
UPSERT_BATCH_SIZE = 1_000

_TRANSACTION_FIELDS = (
    "account_id",
    "posted_on",
    "amount",
    "description",
    "vendor",
    "category",
)


def upsert_entity(session: Session, entity: Entity) -> None:
    stmt = insert(orm.Entity).values(entity.model_dump())
    stmt = stmt.on_conflict_do_update(
        index_elements=[orm.Entity.id],
        set_={
            "name": stmt.excluded.name,
            "currency": stmt.excluded.currency,
            "opening_balance": stmt.excluded.opening_balance,
            "opening_balance_on": stmt.excluded.opening_balance_on,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)


def upsert_transactions(
    session: Session, transactions: Iterable[Transaction], *, source_updated_at: datetime
) -> int:
    """Insert or update by `(entity_id, source_id)`. Returns how many rows were written.

    `source_updated_at` is when upstream emitted this version. A row is only replaced by a
    version at least as new, so out-of-order deliveries can't roll data back. Re-sending
    identical data writes nothing, so `updated_at` only moves on real changes.
    """
    written = 0
    for batch in batched(transactions, UPSERT_BATCH_SIZE, strict=False):
        written += _upsert_transaction_batch(session, batch, source_updated_at)
    return written


def _upsert_transaction_batch(
    session: Session, batch: Sequence[Transaction], source_updated_at: datetime
) -> int:
    rows = [{**t.model_dump(), "source_updated_at": source_updated_at} for t in batch]
    stmt = insert(orm.Transaction).values(rows)
    current = tuple_(*(getattr(orm.Transaction, f) for f in _TRANSACTION_FIELDS))
    incoming = tuple_(*(getattr(stmt.excluded, f) for f in _TRANSACTION_FIELDS))
    upsert = stmt.on_conflict_do_update(
        constraint="uq_transactions_entity_source",
        set_={
            **{f: getattr(stmt.excluded, f) for f in _TRANSACTION_FIELDS},
            "source_updated_at": stmt.excluded.source_updated_at,
            "updated_at": func.now(),
        },
        where=current.is_distinct_from(incoming)
        & (orm.Transaction.source_updated_at <= stmt.excluded.source_updated_at),
    ).returning(orm.Transaction.id)
    return len(session.execute(upsert).all())


def record_inbound_event(session: Session, envelope: RelayEnvelope) -> int | None:
    """Claim a relay event for processing. Returns its row id, or None if already received.

    A concurrent delivery of the same event blocks on the unique index until the first
    transaction finishes, then sees the conflict, so each event is processed once.
    """
    stmt = (
        insert(orm.InboundEvent)
        .values(relay_event_id=envelope.id, type=envelope.type, payload=envelope.data)
        .on_conflict_do_nothing(index_elements=[orm.InboundEvent.relay_event_id])
        .returning(orm.InboundEvent.id)
    )
    return session.execute(stmt).scalar_one_or_none()


def inbound_event_status(session: Session, relay_event_id: str) -> InboundEventStatus:
    stmt = select(orm.InboundEvent.status).where(orm.InboundEvent.relay_event_id == relay_event_id)
    return session.execute(stmt).scalar_one()


def finish_inbound_event(
    session: Session,
    event_id: int,
    status: InboundEventStatus,
    *,
    entity_id: str | None,
    error: str | None,
) -> None:
    session.execute(
        update(orm.InboundEvent)
        .where(orm.InboundEvent.id == event_id)
        .values(status=status, entity_id=entity_id, error=error, processed_at=func.now())
    )


def entity_transactions(session: Session, entity_id: str) -> list[Transaction]:
    rows = session.scalars(
        select(orm.Transaction)
        .where(orm.Transaction.entity_id == entity_id)
        .order_by(orm.Transaction.posted_on, orm.Transaction.id)
    )
    return [Transaction.model_validate(row) for row in rows]


def replace_recurring_series(
    session: Session, entity_id: str, series: Sequence[DetectedSeries]
) -> None:
    """Swap an entity's series for a fresh detection run. Ids are not stable across runs."""
    session.execute(delete(orm.RecurringSeries).where(orm.RecurringSeries.entity_id == entity_id))
    if series:
        session.execute(
            insert(orm.RecurringSeries),
            [
                {
                    "entity_id": entity_id,
                    "vendor": s.vendor,
                    "typical_amount": s.typical_amount,
                    "cadence": s.cadence,
                    "next_expected_on": s.next_expected_on,
                    "last_seen_on": s.last_seen_on,
                    "anchor_day": s.anchor_day,
                }
                for s in series
            ],
        )


def entity_history(session: Session, entity_id: str) -> History | None:
    entity = session.get(orm.Entity, entity_id)
    if entity is None:
        return None
    return History(
        transactions=entity_transactions(session, entity_id),
        opening_balance=entity.opening_balance,
        opening_balance_on=entity.opening_balance_on,
    )


def save_forecast(session: Session, entity_id: str, run: ForecastRun) -> None:
    """Replace the entity's forecasts with this run: one row per horizon, points cascade."""
    session.execute(delete(orm.Forecast).where(orm.Forecast.entity_id == entity_id))
    for horizon in run.horizons:
        forecast_id = session.execute(
            insert(orm.Forecast)
            .values(
                entity_id=entity_id,
                horizon_days=horizon.horizon_days,
                as_of=run.as_of,
                starting_balance=run.starting_balance,
                model=run.model.value,
                backtest_mase=horizon.backtest.mase if horizon.backtest else None,
                backtest_coverage=horizon.backtest.coverage if horizon.backtest else None,
                backtest_balance_error=(
                    Decimal(horizon.backtest.balance_error).quantize(Decimal("0.01"))
                    if horizon.backtest
                    else None
                ),
            )
            .returning(orm.Forecast.id)
        ).scalar_one()
        session.execute(
            insert(orm.ForecastPoint),
            [
                {
                    "forecast_id": forecast_id,
                    "on_date": p.on_date,
                    "expected_balance": p.expected,
                    "lower": p.lower,
                    "upper": p.upper,
                }
                for p in horizon.points
            ],
        )


def latest_forecast(session: Session, entity_id: str, horizon_days: int) -> orm.Forecast | None:
    return session.scalars(
        select(orm.Forecast)
        .where(orm.Forecast.entity_id == entity_id, orm.Forecast.horizon_days == horizon_days)
        .order_by(orm.Forecast.generated_at.desc(), orm.Forecast.id.desc())
        .options(selectinload(orm.Forecast.points))
        .limit(1)
    ).one_or_none()


def mark_dirty(session: Session, entity_id: str) -> None:
    """Flag the entity for recompute, keeping the earliest pending time."""
    session.execute(
        update(orm.Entity)
        .where(orm.Entity.id == entity_id, orm.Entity.dirty_since.is_(None))
        .values(dirty_since=func.now())
    )


def claim_dirty(session: Session, entity_id: str) -> bool:
    """Atomically clear the flag. Exactly one concurrent caller gets True per dirty period."""
    claimed = session.execute(
        update(orm.Entity)
        .where(orm.Entity.id == entity_id, orm.Entity.dirty_since.is_not(None))
        .values(dirty_since=None)
        .returning(orm.Entity.id)
    ).scalar_one_or_none()
    return claimed is not None


def dirty_entity_ids(session: Session) -> list[str]:
    stmt = select(orm.Entity.id).where(orm.Entity.dirty_since.is_not(None)).order_by(orm.Entity.id)
    return list(session.scalars(stmt))


def entity_ids(session: Session) -> list[str]:
    return list(session.scalars(select(orm.Entity.id).order_by(orm.Entity.id)))


@dataclass(frozen=True)
class AnomalySyncResult:
    upserted: int
    resolved: int


def sync_anomalies(
    session: Session, entity_id: str, findings: Sequence[Finding]
) -> AnomalySyncResult:
    """Apply a scan's findings idempotently, keyed by fingerprint.

    - New findings are inserted as open.
    - Open anomalies found again get their severity, explanation, and evidence refreshed.
    - Dismissed and resolved anomalies are never touched, so a rescan can't reopen them.
    - Open missed-bill anomalies that this scan no longer reports are resolved if the bill has
      since arrived (a later charge from that vendor in that direction).
    """
    ids_by_source = _transaction_ids(
        session, entity_id, {i for f in findings for i in f.source_ids}
    )
    for f in findings:
        stmt = insert(orm.Anomaly).values(
            entity_id=entity_id,
            type=f.type,
            severity=f.severity,
            explanation=f.explanation,
            transaction_ids=[ids_by_source[i] for i in f.source_ids],
            fingerprint=f.fingerprint,
        )
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_anomalies_entity_fingerprint",
                set_={
                    "severity": stmt.excluded.severity,
                    "explanation": stmt.excluded.explanation,
                    "transaction_ids": stmt.excluded.transaction_ids,
                },
                where=orm.Anomaly.status == AnomalyStatus.OPEN,
            )
        )
    resolved = _resolve_arrived_bills(session, entity_id, {f.fingerprint for f in findings})
    return AnomalySyncResult(upserted=len(findings), resolved=resolved)


def _transaction_ids(session: Session, entity_id: str, source_ids: set[str]) -> dict[str, int]:
    if not source_ids:
        return {}
    rows = session.execute(
        select(orm.Transaction.source_id, orm.Transaction.id).where(
            orm.Transaction.entity_id == entity_id, orm.Transaction.source_id.in_(source_ids)
        )
    )
    return dict(rows.all())


def _resolve_arrived_bills(session: Session, entity_id: str, still_found: set[str]) -> int:
    stale = session.scalars(
        select(orm.Anomaly).where(
            orm.Anomaly.entity_id == entity_id,
            orm.Anomaly.type == AnomalyType.MISSED_RECURRING,
            orm.Anomaly.status == AnomalyStatus.OPEN,
            orm.Anomaly.fingerprint.not_in(still_found),
        )
    ).all()
    resolved = 0
    for anomaly in stale:
        last_seen = (
            session.get(orm.Transaction, anomaly.transaction_ids[0])
            if anomaly.transaction_ids
            else None
        )
        if last_seen is None:
            continue
        same_direction = (
            orm.Transaction.amount > 0 if last_seen.amount > 0 else orm.Transaction.amount < 0
        )
        arrived = session.scalar(
            select(func.count())
            .select_from(orm.Transaction)
            .where(
                orm.Transaction.entity_id == entity_id,
                orm.Transaction.vendor == last_seen.vendor,
                orm.Transaction.posted_on > last_seen.posted_on,
                same_direction,
            )
        )
        if arrived:
            anomaly.status = AnomalyStatus.RESOLVED
            resolved += 1
    session.flush()
    return resolved


def list_anomalies(session: Session, entity_id: str, status: AnomalyStatus) -> list[orm.Anomaly]:
    return list(
        session.scalars(
            select(orm.Anomaly).where(
                orm.Anomaly.entity_id == entity_id, orm.Anomaly.status == status
            )
        )
    )


def get_anomaly(session: Session, entity_id: str, anomaly_id: int) -> orm.Anomaly | None:
    """Scoped by entity: another entity's anomaly id is simply not found."""
    return session.scalars(
        select(orm.Anomaly).where(orm.Anomaly.entity_id == entity_id, orm.Anomaly.id == anomaly_id)
    ).one_or_none()


def prune_transactions(session: Session, entity_id: str, keep_source_ids: set[str]) -> int:
    """Delete the entity's transactions not in `keep_source_ids`; for sources that own the
    entity's full history (the demo seeder). Returns how many were deleted."""
    deleted = session.execute(
        delete(orm.Transaction)
        .where(
            orm.Transaction.entity_id == entity_id,
            orm.Transaction.source_id.not_in(keep_source_ids),
        )
        .returning(orm.Transaction.id)
    )
    return len(deleted.all())


def delete_anomalies(session: Session, entity_id: str) -> None:
    session.execute(delete(orm.Anomaly).where(orm.Anomaly.entity_id == entity_id))
