from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow.api.app import create_app
from cashflow.core.enums import AnomalyStatus, AnomalyType
from cashflow.core.models import Transaction
from cashflow.core.refresh import refresh_entity
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.demo.seed import seed
from cashflow.settings import Settings, WebhookSettings

pytestmark = pytest.mark.db

AS_OF = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 15, tzinfo=UTC)
SETTINGS = Settings(_env_file=None, webhook=WebhookSettings(secret=SecretStr("s")))


@pytest.fixture
def client(session: Session, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    seed(session, entities=2, base_seed=1, as_of=AS_OF, settings=SETTINGS)
    session.commit()
    app = create_app(SETTINGS, session_factory=session_factory, clock=lambda: NOW)
    with TestClient(app) as client:
        yield client


def anomalies(client: TestClient, entity_id: str = "demo-1", **params: str) -> list[dict[str, Any]]:
    response = client.get(f"/entities/{entity_id}/anomalies", params=params)
    assert response.status_code == 200
    body: list[dict[str, Any]] = response.json()
    return body


def of_type(items: list[dict[str, Any]], kind: AnomalyType) -> dict[str, Any]:
    (item,) = [a for a in items if a["type"] == kind]
    return item


def stored_count(session: Session, entity_id: str = "demo-1") -> int:
    stmt = select(func.count()).select_from(orm.Anomaly).where(orm.Anomaly.entity_id == entity_id)
    return session.scalar(stmt) or 0


def test_lists_open_anomalies_most_severe_first(client: TestClient, session: Session) -> None:
    items = anomalies(client)

    assert sorted(a["type"] for a in items) == sorted(AnomalyType)
    rank = {"high": 0, "medium": 1, "low": 2}
    assert [rank[a["severity"]] for a in items] == sorted(rank[a["severity"]] for a in items)
    assert all(a["status"] == "open" and a["dismissed_at"] is None for a in items)
    evidence = {i for a in items for i in a["transaction_ids"]}
    stmt = select(orm.Transaction.id).where(
        orm.Transaction.id.in_(evidence), orm.Transaction.entity_id == "demo-1"
    )
    assert set(session.scalars(stmt)) == evidence, "evidence points at this entity's rows"


def test_dismissing_moves_an_anomaly_out_of_the_open_list(client: TestClient) -> None:
    target = of_type(anomalies(client), AnomalyType.DUPLICATE_CHARGE)

    response = client.patch(
        f"/entities/demo-1/anomalies/{target['id']}", json={"status": "dismissed"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "dismissed"
    assert response.json()["dismissed_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert target["id"] not in {a["id"] for a in anomalies(client)}
    assert [a["id"] for a in anomalies(client, status="dismissed")] == [target["id"]]


def test_dismissal_can_be_undone(client: TestClient) -> None:
    target = of_type(anomalies(client), AnomalyType.DUPLICATE_CHARGE)
    url = f"/entities/demo-1/anomalies/{target['id']}"
    client.patch(url, json={"status": "dismissed"})

    reopened = client.patch(url, json={"status": "open"}).json()

    assert reopened["status"] == "open"
    assert reopened["dismissed_at"] is None


def test_rescans_neither_duplicate_nor_reopen(client: TestClient, session: Session) -> None:
    target = of_type(anomalies(client), AnomalyType.NEW_VENDOR_LARGE)
    client.patch(f"/entities/demo-1/anomalies/{target['id']}", json={"status": "dismissed"})
    before = stored_count(session)

    refresh_entity(session, "demo-1", as_of=AS_OF, settings=SETTINGS)
    refresh_entity(session, "demo-1", as_of=AS_OF, settings=SETTINGS)

    assert stored_count(session) == before
    assert target["id"] not in {a["id"] for a in anomalies(client)}


def test_late_bill_resolves_its_missed_anomaly(client: TestClient, session: Session) -> None:
    missed = of_type(anomalies(client), AnomalyType.MISSED_RECURRING)
    late = Transaction(
        entity_id="demo-1",
        source_id="late-google-workspace",
        account_id="demo-1-card",
        posted_on=AS_OF,
        amount=Decimal("-72.00"),
        description="POS GOOGLE WORKSPACE",
        vendor="Google Workspace",
        category="Software & Subscriptions",
    )
    repositories.upsert_transactions(session, [late], source_updated_at=NOW)

    refresh_entity(session, "demo-1", as_of=AS_OF, settings=SETTINGS)

    resolved = anomalies(client, status="resolved")
    assert [a["id"] for a in resolved] == [missed["id"]]
    conflict = client.patch(f"/entities/demo-1/anomalies/{missed['id']}", json={"status": "open"})
    assert conflict.status_code == 409


def test_anomalies_are_scoped_to_their_entity(client: TestClient) -> None:
    theirs = anomalies(client, "demo-2")[0]

    response = client.patch(
        f"/entities/demo-1/anomalies/{theirs['id']}", json={"status": "dismissed"}
    )

    assert response.status_code == 404
    assert {a["id"] for a in anomalies(client)}.isdisjoint(
        a["id"] for a in anomalies(client, "demo-2")
    )


def test_unknown_entity_is_not_found(client: TestClient) -> None:
    assert client.get("/entities/nobody/anomalies").status_code == 404


@pytest.mark.parametrize("status", ["resolved", "deleted"])
def test_only_open_and_dismissed_can_be_set(client: TestClient, status: str) -> None:
    target = anomalies(client)[0]

    response = client.patch(f"/entities/demo-1/anomalies/{target['id']}", json={"status": status})

    assert response.status_code == 422
    assert AnomalyStatus.OPEN in {a["status"] for a in anomalies(client)}
