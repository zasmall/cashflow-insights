"""List anomalies and dismiss (or reopen) them. `resolved` is set only by rescans."""

from http import HTTPStatus
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cashflow.api.deps import ContextDep, SessionDep
from cashflow.core import queries
from cashflow.core.enums import AnomalyStatus
from cashflow.core.queries import AnomalyOut

router = APIRouter(tags=["anomalies"])


class AnomalyUpdate(BaseModel):
    status: Literal[AnomalyStatus.OPEN, AnomalyStatus.DISMISSED]


@router.get(
    "/entities/{entity_id}/anomalies",
    responses={HTTPStatus.NOT_FOUND: {"description": "Unknown entity"}},
)
def list_anomalies(
    entity_id: str, session: SessionDep, status: AnomalyStatus = AnomalyStatus.OPEN
) -> list[AnomalyOut]:
    """Most severe first, then most recently detected."""
    return queries.list_anomalies(session, entity_id, status)


@router.patch(
    "/entities/{entity_id}/anomalies/{anomaly_id}",
    responses={
        HTTPStatus.NOT_FOUND: {"description": "No such anomaly for this entity"},
        HTTPStatus.CONFLICT: {"description": "Resolved anomalies can't be changed"},
    },
)
def update_anomaly(
    entity_id: str,
    anomaly_id: int,
    update: AnomalyUpdate,
    session: SessionDep,
    context: ContextDep,
) -> AnomalyOut:
    anomaly = queries.get_anomaly(session, entity_id, anomaly_id)
    if anomaly.status is AnomalyStatus.RESOLVED:
        raise HTTPException(HTTPStatus.CONFLICT, "resolved anomalies can't be changed")

    if anomaly.status is not update.status:
        anomaly.status = update.status
        anomaly.dismissed_at = context.clock() if update.status is AnomalyStatus.DISMISSED else None
        session.commit()
    return AnomalyOut.of(anomaly)
