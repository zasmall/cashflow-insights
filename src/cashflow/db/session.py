"""Engine and session factories. Nothing here holds module-level state."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, Pool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from cashflow.settings import Settings


def make_engine(settings: Settings, *, poolclass: type[Pool] | None = None) -> Engine:
    return create_engine(
        str(settings.database_url),
        echo=settings.database_echo,
        pool_pre_ping=True,
        poolclass=poolclass,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_read_only_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessions whose transactions Postgres runs READ ONLY: any write fails at the database."""
    return sessionmaker(
        bind=engine.execution_options(postgresql_readonly=True), expire_on_commit=False
    )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on error."""
    with factory() as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
