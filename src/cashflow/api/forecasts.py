"""`GET /entities/{entity_id}/forecast`: the latest stored balance forecast."""

from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cashflow.api.deps import SessionDep, SettingsDep
from cashflow.db import models as orm
from cashflow.db import repositories

router = APIRouter(tags=["forecasts"])


class ForecastPointOut(BaseModel):
    date: date
    expected_balance: Decimal
    lower: Decimal
    upper: Decimal


class BacktestOut(BaseModel):
    mase: float | None
    """Daily net-flow error relative to a same-weekday-last-week guess; below 1 beats it."""
    coverage: float | None
    """Share of backtest days the real balance stayed inside the band."""
    balance_error: Decimal | None
    """Typical gap between forecast and actual balance over this horizon, in currency."""


class ForecastOut(BaseModel):
    entity_id: str
    horizon_days: int
    as_of: date
    generated_at: datetime
    starting_balance: Decimal
    confidence_level: int
    model: str
    backtest: BacktestOut
    points: list[ForecastPointOut]


@router.get(
    "/entities/{entity_id}/forecast",
    responses={HTTPStatus.NOT_FOUND: {"description": "Unknown entity, or no forecast yet"}},
)
def get_forecast(
    entity_id: str, session: SessionDep, settings: SettingsDep, horizon: int = 30
) -> ForecastOut:
    if horizon not in settings.forecast.horizons_days:
        allowed = ", ".join(str(h) for h in sorted(settings.forecast.horizons_days))
        raise HTTPException(HTTPStatus.UNPROCESSABLE_CONTENT, f"horizon must be one of {allowed}")
    if session.get(orm.Entity, entity_id) is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "entity not found")
    forecast = repositories.latest_forecast(session, entity_id, horizon)
    if forecast is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "no forecast yet for this entity")

    return ForecastOut(
        entity_id=entity_id,
        horizon_days=forecast.horizon_days,
        as_of=forecast.as_of,
        generated_at=forecast.generated_at,
        starting_balance=forecast.starting_balance,
        confidence_level=settings.forecast.confidence_level,
        model=forecast.model,
        backtest=BacktestOut(
            mase=forecast.backtest_mase,
            coverage=forecast.backtest_coverage,
            balance_error=forecast.backtest_balance_error,
        ),
        points=[
            ForecastPointOut(
                date=p.on_date, expected_balance=p.expected_balance, lower=p.lower, upper=p.upper
            )
            for p in forecast.points
        ],
    )
