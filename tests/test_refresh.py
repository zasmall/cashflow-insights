from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cashflow.core.refresh import refresh_recurring
from cashflow.db import models as orm
from cashflow.db.repositories import upsert_entity, upsert_transactions
from cashflow.demo.generator import generate
from cashflow.settings import RecurringSettings

pytestmark = pytest.mark.db

AS_OF = date(2026, 10, 8)
SETTINGS = RecurringSettings()


def load(session: Session, seed: int) -> str:
    ds = generate(seed, AS_OF)
    upsert_entity(session, ds.entity)
    upsert_transactions(session, ds.transactions, source_updated_at=datetime.now(UTC))
    return ds.entity.id


def stored_vendors(session: Session, entity_id: str) -> list[str]:
    return list(
        session.scalars(
            select(orm.RecurringSeries.vendor)
            .where(orm.RecurringSeries.entity_id == entity_id)
            .order_by(orm.RecurringSeries.vendor)
        )
    )


def test_refresh_stores_detected_series(session: Session) -> None:
    entity_id = load(session, 1)

    series = refresh_recurring(session, entity_id, as_of=AS_OF, settings=SETTINGS)

    assert stored_vendors(session, entity_id) == sorted(s.vendor for s in series)
    assert "Adobe" in stored_vendors(session, entity_id)


def test_refresh_replaces_rather_than_appends(session: Session) -> None:
    entity_id = load(session, 1)
    first = refresh_recurring(session, entity_id, as_of=AS_OF, settings=SETTINGS)

    refresh_recurring(session, entity_id, as_of=AS_OF, settings=SETTINGS)

    assert len(stored_vendors(session, entity_id)) == len(first)


def test_refresh_only_touches_its_entity(session: Session) -> None:
    mine, theirs = load(session, 1), load(session, 2)
    refresh_recurring(session, theirs, as_of=AS_OF, settings=SETTINGS)
    before = stored_vendors(session, theirs)

    refresh_recurring(session, mine, as_of=AS_OF, settings=SETTINGS)

    assert stored_vendors(session, theirs) == before
