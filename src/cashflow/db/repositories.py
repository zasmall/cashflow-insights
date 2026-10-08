"""Persistence operations. Callers own the transaction: nothing here commits."""

from collections.abc import Iterable, Sequence
from itertools import batched

from sqlalchemy import func, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cashflow.core.models import Entity, Transaction
from cashflow.db import models as orm

# Postgres caps a statement at 65,535 bind parameters; 9 columns x 1,000 rows stays well under.
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


def upsert_transactions(session: Session, transactions: Iterable[Transaction]) -> int:
    """Insert or update by `(entity_id, source_id)`. Returns how many rows were written.

    Re-sending identical data writes nothing, so `updated_at` only moves on real changes.
    """
    written = 0
    for batch in batched(transactions, UPSERT_BATCH_SIZE, strict=False):
        written += _upsert_transaction_batch(session, batch)
    return written


def _upsert_transaction_batch(session: Session, batch: Sequence[Transaction]) -> int:
    stmt = insert(orm.Transaction).values([t.model_dump() for t in batch])
    current = tuple_(*(getattr(orm.Transaction, f) for f in _TRANSACTION_FIELDS))
    incoming = tuple_(*(getattr(stmt.excluded, f) for f in _TRANSACTION_FIELDS))
    upsert = stmt.on_conflict_do_update(
        constraint="uq_transactions_entity_source",
        set_={
            **{f: getattr(stmt.excluded, f) for f in _TRANSACTION_FIELDS},
            "updated_at": func.now(),
        },
        where=current.is_distinct_from(incoming),
    ).returning(orm.Transaction.id)
    return len(session.execute(upsert).all())
