from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cashflow.db import models as orm
from cashflow.demo.seed import seed
from cashflow.settings import Settings

pytestmark = pytest.mark.db

AS_OF = date(2026, 10, 8)


def test_seed_loads_each_entity(session: Session) -> None:
    results = seed(session, entities=2, base_seed=1, as_of=AS_OF, settings=Settings(_env_file=None))

    assert [r.entity_id for r in results] == ["demo-1", "demo-2"]
    assert len({r.name for r in results}) == 2
    stored = session.scalar(select(func.count()).select_from(orm.Transaction))
    assert stored == sum(r.transactions for r in results)


def test_reseeding_writes_nothing(session: Session) -> None:
    seed(session, entities=2, base_seed=1, as_of=AS_OF, settings=Settings(_env_file=None))

    again = seed(session, entities=2, base_seed=1, as_of=AS_OF, settings=Settings(_env_file=None))

    assert [r.written for r in again] == [0, 0]


def test_seed_detects_recurring_series(session: Session) -> None:
    results = seed(session, entities=1, base_seed=1, as_of=AS_OF, settings=Settings(_env_file=None))

    stored = session.scalar(select(func.count()).select_from(orm.RecurringSeries))
    assert stored == results[0].recurring_series > 0
