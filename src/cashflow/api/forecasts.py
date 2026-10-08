"""`GET /entities/{entity_id}/forecast`: the latest stored balance forecast."""

from http import HTTPStatus

from fastapi import APIRouter

from cashflow.api.deps import SessionDep, SettingsDep
from cashflow.core import queries
from cashflow.core.queries import ForecastOut

router = APIRouter(tags=["forecasts"])


@router.get(
    "/entities/{entity_id}/forecast",
    responses={
        HTTPStatus.NOT_FOUND: {"description": "Unknown entity, or no forecast yet"},
        HTTPStatus.UNPROCESSABLE_CONTENT: {"description": "Horizon isn't configured"},
    },
)
def get_forecast(
    entity_id: str, session: SessionDep, settings: SettingsDep, horizon: int = 30
) -> ForecastOut:
    return queries.get_forecast(session, entity_id, horizon, settings)
