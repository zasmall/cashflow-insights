import pytest
from sqlalchemy import Engine, text

from cashflow.db.session import make_session_factory, session_scope

pytestmark = pytest.mark.db


def test_session_round_trip(engine: Engine) -> None:
    factory = make_session_factory(engine)

    with session_scope(factory) as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    factory = make_session_factory(engine)

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        # Postgres DDL is transactional, so a rolled-back CREATE leaves no table behind.
        session.execute(text("CREATE TABLE rollback_probe (id int)"))
        raise RuntimeError("boom")

    with session_scope(factory) as session:
        exists = session.execute(text("SELECT to_regclass('public.rollback_probe')")).scalar_one()
        assert exists is None
