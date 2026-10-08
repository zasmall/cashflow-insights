"""Request-scoped dependencies, all derived from the `AppContext` built in `create_app`."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from cashflow.settings import Settings

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    clock: Clock


def get_context(request: Request) -> AppContext:
    context = request.app.state.context
    if not isinstance(context, AppContext):
        raise TypeError("app.state.context is not configured; build the app with create_app()")
    return context


ContextDep = Annotated[AppContext, Depends(get_context)]


def get_settings(context: ContextDep) -> Settings:
    return context.settings


def get_clock(context: ContextDep) -> Clock:
    return context.clock


def get_session(context: ContextDep) -> Iterator[Session]:
    """A session per request. Endpoints commit explicitly; anything uncommitted rolls back."""
    with context.session_factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SessionDep = Annotated[Session, Depends(get_session)]
