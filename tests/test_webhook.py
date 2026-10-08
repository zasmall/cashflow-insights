import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow.api.app import create_app
from cashflow.core.events import TRANSACTION_CATEGORIZED
from cashflow.core.models import Entity
from cashflow.core.signature import sign
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.settings import Settings, WebhookSettings
from tests.helpers import GOLDEN_BODY, GOLDEN_HEADER, GOLDEN_TIMESTAMP

pytestmark = pytest.mark.db

SECRET = "whsec_test"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
ENTITY_ID = "17"


def make_settings(**webhook: Any) -> Settings:
    return Settings(_env_file=None, webhook=WebhookSettings(secret=SecretStr(SECRET), **webhook))


@pytest.fixture
def client(session: Session, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
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
    app = create_app(make_settings(), session_factory=session_factory, clock=lambda: NOW)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def payload(
    source_id: str = "48213",
    *,
    entity_id: str = ENTITY_ID,
    amount: object = "-129.99",
    currency: str = "USD",
    category: str = "Software & Subscriptions",
    categorized_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "transaction": {
            "id": source_id,
            "account_id": "3",
            "posted_on": "2026-10-01",
            "amount": amount,
            "currency": currency,
            "description": "ADOBE *CREATIVE CLD",
            "vendor": "Adobe",
            "category": category,
            "categorized_at": categorized_at.isoformat(),
        },
    }


def envelope(
    data: dict[str, Any] | None = None,
    *,
    event_id: str = "evt-1",
    event_type: str = TRANSACTION_CATEGORIZED,
    created_at: datetime = NOW,
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "created_at": created_at.isoformat(),
            "data": payload() if data is None else data,
        }
    ).encode()


def deliver(
    client: TestClient,
    body: bytes,
    *,
    secret: str = SECRET,
    timestamp: int | None = None,
    signature: str | None = None,
) -> httpx2.Response:
    t = int(NOW.timestamp()) if timestamp is None else timestamp
    headers = {"Content-Type": "application/json"}
    headers["X-Relay-Signature"] = signature or f"t={t},v1={sign(body, secret, t)}"
    return client.post("/webhooks/relay", content=body, headers=headers)


def events(session: Session) -> list[orm.InboundEvent]:
    return list(session.scalars(select(orm.InboundEvent).order_by(orm.InboundEvent.id)))


def stored(session: Session, source_id: str = "48213") -> orm.Transaction | None:
    return session.scalars(
        select(orm.Transaction).where(orm.Transaction.source_id == source_id)
    ).one_or_none()


def transaction_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(orm.Transaction)) or 0


# Happy path and dedupe ---------------------------------------------------------------------


def test_valid_event_is_processed(client: TestClient, session: Session) -> None:
    response = deliver(client, envelope())

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "duplicate": False}
    txn = stored(session)
    assert txn is not None
    assert txn.amount == Decimal("-129.99")
    assert txn.source_updated_at == NOW
    (event,) = events(session)
    assert (event.status, event.entity_id, event.error) == ("processed", ENTITY_ID, None)
    assert event.processed_at is not None


def test_redelivery_is_acknowledged_without_reprocessing(
    client: TestClient, session: Session
) -> None:
    deliver(client, envelope())

    response = deliver(client, envelope())

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "duplicate": True}
    assert len(events(session)) == 1
    assert transaction_count(session) == 1


def test_versions_follow_the_source_not_the_relay(client: TestClient, session: Session) -> None:
    """A categorization made first but delivered last (after retries) carries the newest relay
    `created_at`. The source's `categorized_at` decides, so it can't undo the later one."""
    first, second = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    deliver(
        client,
        envelope(payload(category="Marketing", categorized_at=second), event_id="evt-2"),
    )

    response = deliver(
        client,
        envelope(
            payload(category="Travel", categorized_at=first),
            event_id="evt-1",
            created_at=NOW + timedelta(minutes=5),  # the relay saw it last
        ),
    )

    assert response.json()["status"] == "processed"
    session.expire_all()
    txn = stored(session)
    assert txn is not None
    assert txn.category == "Marketing"


# Trust failures: 401, nothing stored ---------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"secret": "wrong"}, id="wrong-secret"),
        pytest.param({"timestamp": int(NOW.timestamp()) - 301}, id="stale"),
        pytest.param({"timestamp": int(NOW.timestamp()) + 301}, id="future"),
        pytest.param({"signature": "t=1,v1=nope"}, id="malformed"),
    ],
)
def test_untrusted_requests_are_rejected(
    client: TestClient, session: Session, kwargs: dict[str, Any]
) -> None:
    response = deliver(client, envelope(), **kwargs)

    assert response.status_code == 401
    assert events(session) == []


def test_missing_signature_is_rejected(client: TestClient, session: Session) -> None:
    response = client.post("/webhooks/relay", content=envelope())

    assert response.status_code == 401
    assert events(session) == []


def test_body_altered_after_signing_is_rejected(client: TestClient, session: Session) -> None:
    body = envelope()
    signature = f"t={int(NOW.timestamp())},v1={sign(body, SECRET, int(NOW.timestamp()))}"

    response = deliver(client, body.replace(b"-129.99", b"-1.00"), signature=signature)

    assert response.status_code == 401
    assert events(session) == []


def test_relay_golden_request_is_accepted(session_factory: sessionmaker[Session]) -> None:
    settings = Settings(
        _env_file=None, webhook=WebhookSettings(secret=SecretStr("whsec_golden_rotated"))
    )
    now = datetime.fromtimestamp(GOLDEN_TIMESTAMP, UTC)
    app = create_app(settings, session_factory=session_factory, clock=lambda: now)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/relay",
            content=GOLDEN_BODY,
            headers={"X-Relay-Signature": GOLDEN_HEADER},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "unknown_entity"  # no entity "17" in this test


# Permanent problems: 200 so the relay stops retrying, recorded with a reason -----------------


def test_invalid_payload_is_recorded_not_retried(client: TestClient, session: Session) -> None:
    response = deliver(client, envelope(payload(amount=-129.99)))

    assert response.status_code == 200
    assert response.json()["status"] == "invalid"
    (event,) = events(session)
    assert event.entity_id == ENTITY_ID
    assert event.error is not None
    assert "transaction.amount" in event.error
    assert transaction_count(session) == 0


def test_unknown_entity_is_kept_for_reprocessing(client: TestClient, session: Session) -> None:
    response = deliver(client, envelope(payload(entity_id="99")))

    assert response.json() == {"status": "unknown_entity", "duplicate": False}
    (event,) = events(session)
    assert event.entity_id == "99"
    assert event.payload == payload(entity_id="99")
    assert transaction_count(session) == 0


def test_unsupported_event_type_is_ignored(client: TestClient, session: Session) -> None:
    response = deliver(client, envelope({"anything": 1}, event_type="invoice.paid"))

    assert response.json()["status"] == "ignored"
    (event,) = events(session)
    assert event.error == "unsupported event type 'invoice.paid'"


def test_currency_mismatch_is_invalid(client: TestClient, session: Session) -> None:
    response = deliver(client, envelope(payload(currency="EUR")))

    assert response.json()["status"] == "invalid"
    assert transaction_count(session) == 0


# Transport-level problems --------------------------------------------------------------------


def test_signed_non_envelope_is_bad_request(client: TestClient, session: Session) -> None:
    response = deliver(client, b'{"not": "an envelope"}')

    assert response.status_code == 400
    assert events(session) == []


def test_oversized_body_is_rejected(session_factory: sessionmaker[Session]) -> None:
    app = create_app(make_settings(max_body_bytes=64), session_factory=session_factory)

    with TestClient(app) as client:
        response = deliver(client, envelope())

    assert response.status_code == 413


def test_failure_mid_ingest_commits_nothing_so_retry_succeeds(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: object, **__: object) -> int:
        raise RuntimeError("database fell over")

    with monkeypatch.context() as patch:
        patch.setattr(repositories, "upsert_transactions", explode)
        failed = deliver(client, envelope())

    assert failed.status_code == 500
    assert events(session) == []

    retried = deliver(client, envelope())

    assert retried.json() == {"status": "processed", "duplicate": False}


def test_app_refuses_to_start_without_a_secret() -> None:
    with pytest.raises(ValueError, match="WEBHOOK__SECRET"):
        create_app(Settings(_env_file=None))


def test_processed_event_refreshes_the_entity_in_the_background(
    client: TestClient, session: Session
) -> None:
    deliver(client, envelope())  # TestClient runs background tasks before returning

    session.expire_all()
    entity = session.get(orm.Entity, ENTITY_ID)
    assert entity is not None
    assert entity.dirty_since is None
    assert repositories.latest_forecast(session, ENTITY_ID, 30) is not None


def test_rejected_events_do_not_trigger_a_refresh(client: TestClient, session: Session) -> None:
    deliver(client, envelope(payload(amount=-129.99)))

    assert repositories.latest_forecast(session, ENTITY_ID, 30) is None
