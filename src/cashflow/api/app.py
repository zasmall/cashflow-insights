"""Application factory. `cashflow.api.main` holds the module-level app for `fastapi dev`."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from cashflow.api import anomalies, forecasts, summary, webhooks
from cashflow.api.deps import AppContext, Clock, utc_now
from cashflow.core.errors import InvalidRequestError, NotFoundError
from cashflow.db.session import make_engine, make_session_factory
from cashflow.settings import Settings


def create_app(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session] | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Build the app. Pass `session_factory` to reuse a connection (tests); otherwise one
    engine is created here and disposed on shutdown."""
    if not settings.webhook.secret.get_secret_value():
        raise ValueError("WEBHOOK__SECRET must be set; refusing to accept unsigned webhooks")

    engine = None
    if session_factory is None:
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(title="Cashflow Insights", lifespan=lifespan)
    app.state.context = AppContext(settings, session_factory, clock)
    app.add_exception_handler(NotFoundError, _error_handler(HTTPStatus.NOT_FOUND))
    app.add_exception_handler(InvalidRequestError, _error_handler(HTTPStatus.UNPROCESSABLE_CONTENT))
    app.include_router(webhooks.router)
    app.include_router(forecasts.router)
    app.include_router(anomalies.router)
    app.include_router(summary.router)
    return app


def _error_handler(status: HTTPStatus) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """Core read services raise domain errors; HTTP is only decided here."""

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    return handle
