"""Persistence operations. Callers own the transaction: nothing here commits."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from itertools import batched

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cashflow.core.enums import InboundEventStatus
from cashflow.core.events import RelayEnvelope
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
                }
                for s in series
            ],
        )
