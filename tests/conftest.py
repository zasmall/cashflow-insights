import os
import re
from collections.abc import Iterator

import pytest
from alembic import command
from pydantic import PostgresDsn
from sqlalchemy import URL, Engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from cashflow.db.session import make_engine
from cashflow.settings import Settings
from tests.helpers import alembic_config


@pytest.fixture
def settings() -> Settings:
    """Settings isolated from any developer `.env` file."""
    return Settings(_env_file=None)


def _recreate_database(admin_url: URL, name: str) -> None:
    if not re.fullmatch(r"\w+", name):
        msg = f"refusing to recreate unexpected database name {name!r}"
        raise ValueError(msg)
    admin = make_engine(
        Settings(_env_file=None, database_url=PostgresDsn(admin_url.render_as_string(False)))
    )
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """An engine on a fresh `<db>_test` database, migrated to head once per run.

    Skips locally when Postgres isn't up; fails in CI, where it must be.
    """
    base_url = make_url(str(Settings(_env_file=None).database_url))
    test_url = base_url.set(database=f"{base_url.database}_test")
    try:
        _recreate_database(base_url.set(database="postgres"), str(test_url.database))
    except OperationalError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"PostgreSQL unavailable ({exc.orig}); run `docker compose up -d`")

    engine = make_engine(
        Settings(_env_file=None, database_url=PostgresDsn(test_url.render_as_string(False)))
    )
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work, commits included, is rolled back after the test."""
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()
