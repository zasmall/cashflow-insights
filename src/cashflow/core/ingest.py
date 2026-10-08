"""Turn a verified relay event into stored data.

The caller runs `ingest` inside one database transaction. Recording the event and applying
it commit or roll back together: if processing fails, the dedupe record disappears too, and
the relay's retry is processed normally instead of being mistaken for a duplicate.
"""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.orm import Session

from cashflow.core.enums import InboundEventStatus
from cashflow.core.events import TRANSACTION_CATEGORIZED, RelayEnvelope, TransactionCategorized
from cashflow.db import models as orm
from cashflow.db import repositories

MAX_ERROR_LENGTH = 2_000


@dataclass(frozen=True)
class IngestResult:
    status: InboundEventStatus
    duplicate: bool = False
    """True when this event was received before; `status` is then the original outcome."""
    entity_id: str | None = None


@dataclass(frozen=True)
class _Outcome:
    status: InboundEventStatus
    entity_id: str | None = None
    error: str | None = None


def ingest(session: Session, envelope: RelayEnvelope) -> IngestResult:
    event_id = repositories.record_inbound_event(session, envelope)
    if event_id is None:
        return IngestResult(repositories.inbound_event_status(session, envelope.id), duplicate=True)

    outcome = _apply(session, envelope)
    repositories.finish_inbound_event(
        session, event_id, outcome.status, entity_id=outcome.entity_id, error=outcome.error
    )
    return IngestResult(outcome.status, entity_id=outcome.entity_id)


def _apply(session: Session, envelope: RelayEnvelope) -> _Outcome:
    if envelope.type != TRANSACTION_CATEGORIZED:
        return _Outcome(
            InboundEventStatus.IGNORED, error=f"unsupported event type {envelope.type!r}"
        )

    claimed_entity = envelope.data.get("entity_id")
    entity_id = claimed_entity if isinstance(claimed_entity, str) else None
    try:
        payload = TransactionCategorized.model_validate(envelope.data)
    except ValidationError as exc:
        return _Outcome(InboundEventStatus.INVALID, entity_id, _describe(exc))

    entity = session.get(orm.Entity, payload.entity_id)
    if entity is None:
        return _Outcome(InboundEventStatus.UNKNOWN_ENTITY, payload.entity_id)

    if payload.transaction.currency != entity.currency:
        error = f"currency {payload.transaction.currency} does not match entity's {entity.currency}"
        return _Outcome(InboundEventStatus.INVALID, entity.id, error)

    written = repositories.upsert_transactions(
        session, [payload.to_transaction()], source_updated_at=envelope.created_at
    )
    if written:
        repositories.mark_dirty(session, entity.id)
    return _Outcome(InboundEventStatus.PROCESSED, entity.id)


def _describe(exc: ValidationError) -> str:
    """Compact, stable error text: `transaction.amount: amount must be a decimal string; ...`."""
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
    ]
    return "; ".join(parts)[:MAX_ERROR_LENGTH]
