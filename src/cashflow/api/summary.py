"""`GET /entities/{entity_id}/summary`: the weekly report."""

from datetime import date
from http import HTTPStatus

from fastapi import APIRouter

from cashflow.api.deps import ContextDep, SessionDep
from cashflow.core import queries
from cashflow.core.summary import WeeklySummary

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
    return queries.weekly_summary(
        session,
        entity_id,
        today=context.clock().date(),
        week_of=week_of,
        settings=context.settings,
    )
