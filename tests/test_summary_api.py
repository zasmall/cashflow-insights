from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from cashflow.api.app import create_app
from cashflow.core.models import Entity
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
    repositories.upsert_entity(
        session,
        Entity(
            id="fresh",
            name="Fresh Co",
            currency="USD",
            opening_balance=Decimal("500.00"),
            opening_balance_on=date(2026, 9, 1),
        ),
    )
    repositories.mark_dirty(session, "fresh")
    session.commit()
    app = create_app(SETTINGS, session_factory=session_factory, clock=lambda: NOW)
    with TestClient(app) as client:
        yield client


def test_summary_of_a_seeded_business(client: TestClient) -> None:
    response = client.get("/entities/demo-1/summary")

    assert response.status_code == 200
    body = response.json()
    assert (body["week_start"], body["week_end"]) == ("2026-09-28", "2026-10-04")
    assert body["entity_name"]
    cash = body["cash"]
    assert Decimal(cash["end_balance"]) == Decimal(cash["start_balance"]) + Decimal(
        cash["this_week"]["net"]
    )
    assert [h["horizon_days"] for h in body["outlook"]["horizons"]] == [30, 60, 90]
    assert sum(body["anomalies"]["open_by_severity"].values()) == 5
    assert all(item["vendor"] for item in body["upcoming"])
    assert body["refresh_pending"] is False


def test_week_of_selects_an_earlier_week(client: TestClient) -> None:
    body = client.get("/entities/demo-1/summary", params={"week_of": "2026-09-16"}).json()

    assert (body["week_start"], body["week_end"]) == ("2026-09-14", "2026-09-20")


@pytest.mark.parametrize("week_of", ["2026-10-12", "not-a-date"])
def test_future_or_malformed_week_is_rejected(client: TestClient, week_of: str) -> None:
    response = client.get("/entities/demo-1/summary", params={"week_of": week_of})

    assert response.status_code == 422


def test_unknown_entity_is_not_found(client: TestClient) -> None:
    assert client.get("/entities/nobody/summary").status_code == 404


def test_entity_before_its_first_refresh(client: TestClient) -> None:
    body = client.get("/entities/fresh/summary").json()

    assert body["outlook"] is None
    assert body["refresh_pending"] is True
    assert body["cash"]["start_balance"] == "500.00"


def test_each_entity_gets_its_own_summary(client: TestClient) -> None:
    one = client.get("/entities/demo-1/summary").json()
    two = client.get("/entities/demo-2/summary").json()

    assert one["entity_name"] != two["entity_name"]
    assert {a["id"] for a in one["anomalies"]["top"]}.isdisjoint(
        a["id"] for a in two["anomalies"]["top"]
    )
