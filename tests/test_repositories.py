from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cashflow.core.models import Entity, Transaction
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.db.repositories import upsert_entity

pytestmark = pytest.mark.db


def make_entity(entity_id: str = "ent-1", name: str = "Test Co") -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        currency="USD",
        opening_balance=Decimal("1000.00"),
        opening_balance_on=date(2026, 1, 1),
    )


def make_txn(source_id: str, entity_id: str = "ent-1", amount: str = "-10.00") -> Transaction:
    return Transaction(
        entity_id=entity_id,
        source_id=source_id,
        account_id="acct-1",
        posted_on=date(2026, 1, 15),
        amount=Decimal(amount),
        description="POS ADOBE",
        vendor="Adobe",
        category="Software",
    )


V1 = datetime(2026, 1, 15, 12, tzinfo=UTC)


def upsert(session: Session, txns: Iterable[Transaction], at: datetime = V1) -> int:
    return repositories.upsert_transactions(session, txns, source_updated_at=at)


def stored_amount(session: Session, source_id: str) -> Decimal | None:
    return session.scalar(
        select(orm.Transaction.amount).where(orm.Transaction.source_id == source_id)
    )


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(orm.Transaction)) or 0


@pytest.fixture(autouse=True)
def entities(session: Session) -> None:
    upsert_entity(session, make_entity("ent-1"))
    upsert_entity(session, make_entity("ent-2"))


def test_upsert_inserts_new_rows(session: Session) -> None:
    written = upsert(session, [make_txn("t-1"), make_txn("t-2")])

    assert written == 2
    assert count(session) == 2


def test_resending_identical_rows_writes_nothing(session: Session) -> None:
    upsert(session, [make_txn("t-1")])

    assert upsert(session, [make_txn("t-1")]) == 0


def test_changed_row_is_updated_in_place(session: Session) -> None:
    upsert(session, [make_txn("t-1"), make_txn("t-2")])

    written = upsert(session, [make_txn("t-1", amount="-12.50"), make_txn("t-2")])

    assert written == 1
    assert count(session) == 2
    assert stored_amount(session, "t-1") == Decimal("-12.50")


def test_source_ids_are_scoped_per_entity(session: Session) -> None:
    upsert(session, [make_txn("t-1", "ent-1"), make_txn("t-1", "ent-2")])

    assert count(session) == 2


def test_large_inputs_are_batched(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repositories, "UPSERT_BATCH_SIZE", 3)

    written = upsert(session, (make_txn(f"t-{n}") for n in range(10)))

    assert written == 10
    assert count(session) == 10


def test_upsert_entity_updates_existing(session: Session) -> None:
    upsert_entity(session, make_entity("ent-1", name="Renamed Co"))

    entity = session.get(orm.Entity, "ent-1", populate_existing=True)
    assert entity is not None
    assert entity.name == "Renamed Co"


def test_older_version_never_overwrites_newer(session: Session) -> None:
    upsert(session, [make_txn("t-1", amount="-12.50")], at=V1 + timedelta(minutes=5))

    written = upsert(session, [make_txn("t-1", amount="-10.00")], at=V1)

    assert written == 0
    assert stored_amount(session, "t-1") == Decimal("-12.50")


def test_newer_version_overwrites_older(session: Session) -> None:
    upsert(session, [make_txn("t-1", amount="-10.00")], at=V1)

    written = upsert(session, [make_txn("t-1", amount="-12.50")], at=V1 + timedelta(seconds=1))

    assert written == 1
    assert stored_amount(session, "t-1") == Decimal("-12.50")
