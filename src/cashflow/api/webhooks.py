"""`POST /webhooks/relay`: verified, deduplicated ingest of relay deliveries.

Status codes follow the relay's retry policy: any 2xx is final, 410 would disable the
endpoint, and everything else is retried. So events that can never succeed (unsupported
type, invalid payload, unknown entity) are recorded and answered 200, and only transient
or trust problems get an error status.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from cashflow.api.deps import ClockDep, SessionDep, SettingsDep
from cashflow.core.enums import InboundEventStatus
from cashflow.core.events import RelayEnvelope
from cashflow.core.ingest import ingest
from cashflow.core.signature import SignatureError, verify

router = APIRouter(tags=["webhooks"])


class IngestResponse(BaseModel):
    status: InboundEventStatus
    duplicate: bool


async def raw_body(request: Request, settings: SettingsDep) -> bytes:
    """The exact bytes that were signed, read with a size cap."""
    limit = settings.webhook.max_body_bytes
    too_large = HTTPException(HTTPStatus.CONTENT_TOO_LARGE, f"body exceeds {limit} bytes")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large

    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)


@router.post(
    "/webhooks/relay",
    responses={
        HTTPStatus.BAD_REQUEST: {"description": "Signed body is not a relay envelope"},
        HTTPStatus.UNAUTHORIZED: {"description": "Missing, invalid, or stale signature"},
        HTTPStatus.CONTENT_TOO_LARGE: {"description": "Body over the configured limit"},
    },
)
def receive_relay_event(
    body: Annotated[bytes, Depends(raw_body)],
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    x_relay_signature: Annotated[str | None, Header()] = None,
) -> IngestResponse:
    if x_relay_signature is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "missing X-Relay-Signature header")
    try:
        verify(
            x_relay_signature,
            body,
            settings.webhook.secret.get_secret_value(),
            now=int(clock().timestamp()),
            tolerance_seconds=settings.webhook.signature_tolerance_seconds,
        )
    except SignatureError as exc:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, str(exc)) from exc

    try:
        envelope = RelayEnvelope.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "body is not a valid relay envelope") from exc

    result = ingest(session, envelope)
    session.commit()  # before responding: a 200 must mean the event is durably recorded
    return IngestResponse(status=result.status, duplicate=result.duplicate)
