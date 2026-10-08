"""Provisioning an entity applies the events that arrived before it existed."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cashflow.core.entities import provision_entity
from cashflow.core.enums import InboundEventStatus
from cashflow.core.events import TRANSACTION_CATEGORIZED, RelayEnvelope
from cashflow.core.ingest import ingest
from cashflow.core.models import Entity
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.settings import Settings

pytestmark = pytest.mark.db

SETTINGS = Settings(_env_file=None)
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
ENTITY = Entity(
    id="17",
    name="Acme Ltd",
    currency="USD",
    opening_balance=Decimal("25000.00"),
    opening_balance_on=date(2026, 1, 1),
)


def event(
    event_id: str,
    source_id: str,
    *,
    currency: str = "USD",
    hours_ago: int = 1,
    category: str = "Software & Subscriptions",
) -> RelayEnvelope:
    return RelayEnvelope(
        id=event_id,
        type=TRANSACTION_CATEGORIZED,
        created_at=NOW,
        data={
            "entity_id": ENTITY.id,
            "transaction": {
                "id": source_id,
                "account_id": "3",
                "posted_on": "2026-10-01",
                "amount": "-129.99",
                "currency": currency,
                "description": "ADOBE *CREATIVE CLD",
                "vendor": "Adobe",
                "category": category,
                "categorized_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            },
        },
    )


def statuses(session: Session) -> dict[str, InboundEventStatus]:
    session.expire_all()
    rows = session.execute(select(orm.InboundEvent.relay_event_id, orm.InboundEvent.status))
    return dict(rows.all())


def test_events_wait_for_their_entity_then_apply(session: Session) -> None:
    ingest(session, event("evt-1", "t-1"))
    ingest(session, event("evt-2", "t-2"))
    ingest(session, event("evt-3", "t-3", currency="EUR"))
    assert set(statuses(session).values()) == {InboundEventStatus.UNKNOWN_ENTITY}

    result = provision_entity(session, ENTITY, as_of=NOW.date(), settings=SETTINGS)

    assert result.reprocessed == {InboundEventStatus.PROCESSED: 2, InboundEventStatus.INVALID: 1}
    assert statuses(session) == {
        "evt-1": InboundEventStatus.PROCESSED,
        "evt-2": InboundEventStatus.PROCESSED,
        "evt-3": InboundEventStatus.INVALID,
    }
    assert {t.source_id for t in repositories.entity_transactions(session, ENTITY.id)} == {
        "t-1",
        "t-2",
    }
    assert repositories.latest_forecast(session, ENTITY.id, 30) is not None


def test_replayed_events_keep_their_version_order(session: Session) -> None:
    """Stored events replay oldest-received first, but categorized_at still decides."""
    newer = event("evt-1", "t-1", hours_ago=1)
    older = event("evt-2", "t-1", hours_ago=5, category="Travel")
    ingest(session, newer)
    ingest(session, older)

    provision_entity(session, ENTITY, as_of=NOW.date(), settings=SETTINGS)

    (txn,) = repositories.entity_transactions(session, ENTITY.id)
    assert txn.category == "Software & Subscriptions"


def test_provisioning_again_updates_without_reprocessing(session: Session) -> None:
    provision_entity(session, ENTITY, as_of=NOW.date(), settings=SETTINGS)

    renamed = ENTITY.model_copy(update={"name": "Acme Holdings"})
    result = provision_entity(session, renamed, as_of=NOW.date(), settings=SETTINGS)

    assert result.reprocessed == {}
    entity = session.get(orm.Entity, ENTITY.id)
    assert entity is not None
    assert entity.name == "Acme Holdings"
    assert entity.dirty_since is None
