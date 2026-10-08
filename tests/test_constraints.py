"""The database itself enforces the domain rules, not just the ORM."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cashflow.core.models import Entity
from cashflow.db import models as orm
from cashflow.db.repositories import upsert_entity

pytestmark = pytest.mark.db

ENTITY_ID = "ent-1"


@pytest.fixture(autouse=True)
def entity(session: Session) -> None:
    upsert_entity(
        session,
        Entity(
            id=ENTITY_ID,
            name="Test Co",
            currency="USD",
            opening_balance=Decimal("1000.00"),
            opening_balance_on=date(2026, 1, 1),
        ),
    )


def assert_rejected(session: Session, sql: str, **params: Any) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text(sql), params)


def test_unknown_cadence_is_rejected(session: Session) -> None:
    assert_rejected(
        session,
        "INSERT INTO recurring_series (entity_id, vendor, typical_amount, cadence,"
        " next_expected_on, last_seen_on) VALUES (:e, 'Adobe', -89.99, 'fortnightly',"
        " '2026-02-01', '2026-01-01')",
        e=ENTITY_ID,
    )


def test_relay_event_id_is_unique(session: Session) -> None:
    sql = (
        "INSERT INTO inbound_events (relay_event_id, type, payload)"
        " VALUES ('evt-1', 'transaction.categorized', '{}')"
    )
    session.execute(text(sql))

    assert_rejected(session, sql)


def test_dismissed_at_must_match_status(session: Session) -> None:
    assert_rejected(
        session,
        "INSERT INTO anomalies (entity_id, type, severity, explanation, fingerprint, status)"
        " VALUES (:e, 'duplicate_charge', 'low', 'x', 'fp', 'dismissed')",
        e=ENTITY_ID,
    )


def test_anomaly_fingerprint_is_unique_per_entity(session: Session) -> None:
    sql = (
        "INSERT INTO anomalies (entity_id, type, severity, explanation, fingerprint)"
        " VALUES (:e, 'duplicate_charge', 'low', 'x', 'fp')"
    )
    session.execute(text(sql), {"e": ENTITY_ID})

    assert_rejected(session, sql, e=ENTITY_ID)


def test_forecast_band_must_contain_expected(session: Session) -> None:
    forecast_id = session.execute(
        text(
            "INSERT INTO forecasts (entity_id, horizon_days, as_of, starting_balance, model)"
            " VALUES (:e, 30, '2026-01-31', 1000, 'AutoETS') RETURNING id"
        ),
        {"e": ENTITY_ID},
    ).scalar_one()

    assert_rejected(
        session,
        "INSERT INTO forecast_points (forecast_id, on_date, expected_balance, lower, upper)"
        " VALUES (:f, '2026-02-01', 500, 600, 700)",
        f=forecast_id,
    )


def test_currency_must_be_iso_code(session: Session) -> None:
    assert_rejected(
        session,
        "INSERT INTO entities (id, name, currency, opening_balance, opening_balance_on)"
        " VALUES ('ent-2', 'Bad Co', 'usd', 0, '2026-01-01')",
    )


def test_money_round_trips_exactly(session: Session) -> None:
    session.execute(
        text(
            "INSERT INTO transactions (entity_id, source_id, account_id, posted_on, amount,"
            " description, vendor, category, source_updated_at)"
            " VALUES (:e, 't-1', 'acct', '2026-01-02', :amount, '', 'Adobe', 'Software', now())"
        ),
        {"e": ENTITY_ID, "amount": Decimal("-129.99")},
    )

    amount = session.scalars(select(orm.Transaction.amount)).one()

    assert amount == Decimal("-129.99")
    assert amount.as_tuple().exponent == -2


def test_deleting_entity_cascades(session: Session) -> None:
    session.execute(
        text(
            "INSERT INTO transactions (entity_id, source_id, account_id, posted_on, amount,"
            " description, vendor, category, source_updated_at)"
            " VALUES (:e, 't-1', 'acct', '2026-01-02', -1, '', 'Adobe', 'Software', now())"
        ),
        {"e": ENTITY_ID},
    )

    session.execute(text("DELETE FROM entities WHERE id = :e"), {"e": ENTITY_ID})

    assert session.scalar(text("SELECT count(*) FROM transactions")) == 0
