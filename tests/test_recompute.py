"""Ingest marks entities dirty; refreshes claim them exactly once and survive failures."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cashflow.core import refresh
from cashflow.core.events import TRANSACTION_CATEGORIZED, RelayEnvelope
from cashflow.core.ingest import ingest
from cashflow.core.models import Entity
from cashflow.core.refresh import refresh_if_dirty
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.settings import Settings

pytestmark = pytest.mark.db

SETTINGS = Settings(_env_file=None)
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
ENTITY_ID = "ent-1"


@pytest.fixture(autouse=True)
def entity(session: Session) -> None:
    repositories.upsert_entity(
        session,
        Entity(
            id=ENTITY_ID,
            name="Test Co",
            currency="USD",
            opening_balance=Decimal("1000.00"),
            opening_balance_on=date(2026, 1, 1),
        ),
    )
    session.commit()


def envelope(
    event_id: str, categorized_at: datetime = NOW, amount: str = "-12.00"
) -> RelayEnvelope:
    return RelayEnvelope(
        id=event_id,
        type=TRANSACTION_CATEGORIZED,
        created_at=NOW,
        data={
            "entity_id": ENTITY_ID,
            "transaction": {
                "id": "t-1",
                "account_id": "a",
                "posted_on": "2026-10-01",
                "amount": amount,
                "currency": "USD",
                "description": "",
                "vendor": "Adobe",
                "category": "Software",
                "categorized_at": categorized_at.isoformat(),
            },
        },
    )


def is_dirty(session: Session) -> bool:
    session.expire_all()
    entity = session.get(orm.Entity, ENTITY_ID)
    assert entity is not None
    return entity.dirty_since is not None


def test_new_transaction_marks_entity_dirty(session: Session) -> None:
    ingest(session, envelope("evt-1"))

    assert is_dirty(session)


def test_unchanged_resend_does_not_mark_dirty(session: Session) -> None:
    ingest(session, envelope("evt-1"))
    repositories.claim_dirty(session, ENTITY_ID)

    ingest(session, envelope("evt-2", categorized_at=NOW + timedelta(minutes=1)))

    assert not is_dirty(session)


def test_only_one_claim_wins_per_dirty_period(session: Session) -> None:
    repositories.mark_dirty(session, ENTITY_ID)

    assert repositories.claim_dirty(session, ENTITY_ID) is True
    assert repositories.claim_dirty(session, ENTITY_ID) is False


def test_refresh_if_dirty_refreshes_once(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    ingest(session, envelope("evt-1"))
    session.commit()

    first = refresh_if_dirty(session_factory, ENTITY_ID, as_of=NOW.date(), settings=SETTINGS)
    second = refresh_if_dirty(session_factory, ENTITY_ID, as_of=NOW.date(), settings=SETTINGS)

    assert first is not None
    assert second is None
    assert repositories.latest_forecast(session, ENTITY_ID, 30) is not None
    assert not is_dirty(session)


def test_failed_refresh_leaves_entity_dirty_for_a_retry(
    session: Session, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest(session, envelope("evt-1"))
    session.commit()

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("model blew up")

    monkeypatch.setattr(refresh, "build_forecast", explode)
    with pytest.raises(RuntimeError):
        refresh_if_dirty(session_factory, ENTITY_ID, as_of=NOW.date(), settings=SETTINGS)

    assert is_dirty(session)
    assert repositories.dirty_entity_ids(session) == [ENTITY_ID]
