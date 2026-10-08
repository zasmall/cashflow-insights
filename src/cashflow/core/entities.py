"""Provisioning entities. Ingest never creates them: the opening balance a forecast starts
from has to come from a person, since the categorizer doesn't know balances."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from cashflow.core.enums import InboundEventStatus
from cashflow.core.ingest import reprocess_unknown_entity_events
from cashflow.core.models import Entity
from cashflow.core.refresh import RefreshResult, refresh_entity
from cashflow.db import repositories
from cashflow.settings import Settings


@dataclass(frozen=True)
class ProvisionResult:
    reprocessed: dict[InboundEventStatus, int]
    """Outcomes of the stored events that were waiting for this entity."""
    refresh: RefreshResult


def provision_entity(
    session: Session, entity: Entity, *, as_of: date, settings: Settings
) -> ProvisionResult:
    """Create or update the entity, apply events that arrived before it existed, and refresh
    its derived data. One transaction: the caller commits."""
    repositories.upsert_entity(session, entity)
    outcomes: dict[InboundEventStatus, int] = {}
    for result in reprocess_unknown_entity_events(session, entity.id):
        outcomes[result.status] = outcomes.get(result.status, 0) + 1
    repositories.claim_dirty(session, entity.id)  # refreshing right now, in this transaction
    refresh = refresh_entity(session, entity.id, as_of=as_of, settings=settings)
    return ProvisionResult(reprocessed=outcomes, refresh=refresh)
