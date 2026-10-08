"""`GET /entities/{entity_id}/summary`: the weekly report."""

from datetime import date
from http import HTTPStatus

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from cashflow.api.deps import ContextDep, SessionDep
from cashflow.core.enums import AnomalyStatus
from cashflow.core.forecast import BalancePoint
from cashflow.core.recurring import detect_recurring
from cashflow.core.summary import (
    EntityInfo,
    OpenAnomaly,
    StoredForecast,
    WeeklySummary,
    build_summary,
    summary_week,
)
from cashflow.db import models as orm
from cashflow.db import repositories
from cashflow.settings import Settings

router = APIRouter(tags=["summary"])


@router.get(
    "/entities/{entity_id}/summary",
    responses={
        HTTPStatus.NOT_FOUND: {"description": "Unknown entity"},
        HTTPStatus.UNPROCESSABLE_CONTENT: {"description": "week_of is in the future"},
    },
)
def get_summary(
    entity_id: str, session: SessionDep, context: ContextDep, week_of: date | None = None
) -> WeeklySummary:
    """Last complete Monday-Sunday week by default; `week_of` picks the week containing it."""
    entity = session.get(orm.Entity, entity_id)
    history = repositories.entity_history(session, entity_id)
    if entity is None or history is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "entity not found")

    today = context.clock().date()
    week_start, _ = summary_week(today, week_of)
    if week_start > today:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_CONTENT, "week_of can't be in the future")

    series = detect_recurring(
        history.transactions, as_of=today, settings=context.settings.recurring
    )
    return build_summary(
        EntityInfo(entity.id, entity.name, entity.currency),
        history,
        series,
        _stored_forecast(session, entity_id, context.settings),
        [
            OpenAnomaly(a.id, a.type, a.severity, a.explanation, a.detected_at)
            for a in repositories.list_anomalies(session, entity_id, AnomalyStatus.OPEN)
        ],
        today=today,
        week_start=week_start,
        refresh_pending=entity.dirty_since is not None,
    )


def _stored_forecast(session: Session, entity_id: str, settings: Settings) -> StoredForecast | None:
    by_horizon = {
        h: repositories.latest_forecast(session, entity_id, h)
        for h in settings.forecast.horizons_days
    }
    longest = by_horizon[settings.forecast.max_horizon]
    if longest is None:
        return None
    return StoredForecast(
        as_of=longest.as_of,
        model=longest.model,
        points=[
            BalancePoint(p.on_date, p.expected_balance, p.lower, p.upper) for p in longest.points
        ],
        typical_error={h: f.backtest_balance_error for h, f in by_horizon.items() if f is not None},
    )
