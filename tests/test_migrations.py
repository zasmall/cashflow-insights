import pytest
from alembic import command
from sqlalchemy import Engine, inspect

from cashflow.db.base import Base
from tests.helpers import alembic_config

pytestmark = pytest.mark.db


def test_downgrade_to_base_and_back(engine: Engine) -> None:
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}

        command.upgrade(alembic_config(connection), "head")
        assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())
