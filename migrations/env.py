"""Alembic environment. The database URL comes from `cashflow.settings`, not alembic.ini."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool

from cashflow.db import models  # noqa: F401  (registers tables on Base.metadata)
from cashflow.db.base import Base
from cashflow.db.session import make_engine
from cashflow.settings import Settings

config = context.config

# Tests set configure_logger=False so migrating doesn't reset pytest's log capture.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = Settings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=str(settings.database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_with(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests pass their own connection via `Config.attributes` to migrate a separate database.
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        _run_with(connection)
        return

    engine = make_engine(settings, poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run_with(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
