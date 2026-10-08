import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from cashflow.db.session import make_engine
from cashflow.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings isolated from any developer `.env` file."""
    return Settings(_env_file=None)


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    """A live engine. Skips locally when Postgres isn't up; fails in CI, where it must be."""
    engine = make_engine(settings)
    try:
        engine.connect().close()
    except OperationalError as exc:
        engine.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip(f"PostgreSQL unavailable ({exc.orig}); run `docker compose up -d`")
    yield engine
    engine.dispose()
