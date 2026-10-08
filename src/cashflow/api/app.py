"""Application factory. `cashflow.api.main` holds the module-level app for `fastapi dev`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from cashflow.api import anomalies, forecasts, webhooks
from cashflow.api.deps import AppContext, Clock, utc_now
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
    app.include_router(webhooks.router)
    app.include_router(forecasts.router)
    app.include_router(anomalies.router)
    return app
