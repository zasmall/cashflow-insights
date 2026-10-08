from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow.api.app import create_app
from cashflow.core.enums import ForecastModel
from cashflow.core.models import Entity
from cashflow.core.refresh import refresh_entity
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.demo.seed import seed
from cashflow.settings import Settings, WebhookSettings

pytestmark = pytest.mark.db

AS_OF = date(2026, 10, 8)
SETTINGS = Settings(_env_file=None, webhook=WebhookSettings(secret=SecretStr("s")))


@pytest.fixture(scope="module")
def _warm_statsforecast() -> None:
    import cashflow.core.forecast_models  # noqa: F401, PLC0415  (pay the import once)


@pytest.fixture
def client(
    session: Session, session_factory: sessionmaker[Session], _warm_statsforecast: None
) -> Iterator[TestClient]:
    seed(session, entities=2, base_seed=1, as_of=AS_OF, settings=SETTINGS)
    repositories.upsert_entity(
        session,
        Entity(
            id="fresh",
            name="No Forecast Yet",
            currency="USD",
            opening_balance=Decimal(0),
            opening_balance_on=AS_OF,
        ),
    )
    session.commit()
    with TestClient(create_app(SETTINGS, session_factory=session_factory)) as client:
        yield client


def test_returns_latest_forecast(client: TestClient) -> None:
    response = client.get("/entities/demo-1/forecast")

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == "demo-1"
    assert body["horizon_days"] == 30
    assert body["as_of"] == AS_OF.isoformat()
    assert body["confidence_level"] == 80
    assert body["model"] in set(ForecastModel)
    assert set(body["backtest"]) == {"mase", "coverage", "balance_error"}
    assert len(body["points"]) == 30
    first = body["points"][0]
    assert first["date"] == "2026-10-09"
    assert all(isinstance(first[k], str) for k in ("expected_balance", "lower", "upper"))
    assert Decimal(first["lower"]) <= Decimal(first["expected_balance"]) <= Decimal(first["upper"])


def test_longer_horizon_returns_more_points(client: TestClient) -> None:
    body = client.get("/entities/demo-1/forecast", params={"horizon": 90}).json()

    assert body["horizon_days"] == 90
    assert len(body["points"]) == 90


def test_unconfigured_horizon_is_rejected(client: TestClient) -> None:
    response = client.get("/entities/demo-1/forecast", params={"horizon": 45})

    assert response.status_code == 422
    assert "30, 60, 90" in response.json()["detail"]


def test_unknown_entity_is_not_found(client: TestClient) -> None:
    response = client.get("/entities/nobody/forecast")

    assert response.status_code == 404
    assert response.json()["detail"] == "entity not found"


def test_entity_without_forecast_is_not_found(client: TestClient) -> None:
    response = client.get("/entities/fresh/forecast")

    assert response.status_code == 404
    assert response.json()["detail"] == "no forecast yet for this entity"


def test_each_entity_sees_only_its_own_forecast(client: TestClient, session: Session) -> None:
    one = client.get("/entities/demo-1/forecast").json()
    two = client.get("/entities/demo-2/forecast").json()

    for body in (one, two):
        history = repositories.entity_history(session, body["entity_id"])
        assert history is not None
        assert Decimal(body["starting_balance"]) == history.balance_at(AS_OF)
    assert one["starting_balance"] != two["starting_balance"]


def test_refreshing_replaces_the_previous_run(client: TestClient, session: Session) -> None:
    refresh_entity(session, "demo-1", as_of=AS_OF, settings=SETTINGS)

    stored = session.scalar(
        select(func.count()).select_from(orm.Forecast).where(orm.Forecast.entity_id == "demo-1")
    )
    assert stored == len(SETTINGS.forecast.horizons_days)
