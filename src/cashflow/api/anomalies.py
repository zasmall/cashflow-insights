"""List anomalies and dismiss (or reopen) them. `resolved` is set only by rescans."""

from datetime import datetime
from http import HTTPStatus
from typing import Literal, Self

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cashflow.api.deps import ContextDep, SessionDep
from cashflow.core.enums import AnomalyStatus, AnomalyType, Severity
from cashflow.db import models as orm
from cashflow.db import repositories

router = APIRouter(tags=["anomalies"])

SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


class AnomalyOut(BaseModel):
    id: int
    type: AnomalyType
    severity: Severity
    status: AnomalyStatus
    explanation: str
    transaction_ids: list[int]
    detected_at: datetime
    dismissed_at: datetime | None

    @classmethod
    def of(cls, anomaly: orm.Anomaly) -> Self:
        return cls(
            id=anomaly.id,
            type=anomaly.type,
            severity=anomaly.severity,
            status=anomaly.status,
            explanation=anomaly.explanation,
            transaction_ids=anomaly.transaction_ids,
            detected_at=anomaly.detected_at,
            dismissed_at=anomaly.dismissed_at,
        )


class AnomalyUpdate(BaseModel):
    status: Literal[AnomalyStatus.OPEN, AnomalyStatus.DISMISSED]


def _require_entity(session: SessionDep, entity_id: str) -> None:
    if session.get(orm.Entity, entity_id) is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "entity not found")


@router.get(
    "/entities/{entity_id}/anomalies",
    responses={HTTPStatus.NOT_FOUND: {"description": "Unknown entity"}},
)
def list_anomalies(
    entity_id: str, session: SessionDep, status: AnomalyStatus = AnomalyStatus.OPEN
) -> list[AnomalyOut]:
    """Most severe first, then most recently detected."""
    _require_entity(session, entity_id)
    anomalies = repositories.list_anomalies(session, entity_id, status)
    anomalies.sort(key=lambda a: (SEVERITY_ORDER[a.severity], -a.detected_at.timestamp(), -a.id))
    return [AnomalyOut.of(a) for a in anomalies]


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
    anomaly = repositories.get_anomaly(session, entity_id, anomaly_id)
    if anomaly is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "anomaly not found")
    if anomaly.status is AnomalyStatus.RESOLVED:
        raise HTTPException(HTTPStatus.CONFLICT, "resolved anomalies can't be changed")

    if anomaly.status is not update.status:
        anomaly.status = update.status
        anomaly.dismissed_at = context.clock() if update.status is AnomalyStatus.DISMISSED else None
        session.commit()
    return AnomalyOut.of(anomaly)
