"""Load synthetic demo entities into the database.

    uv run python -m cashflow.demo.seed [--entities 2] [--seed 42] [--as-of 2026-10-08]

Idempotent: generated ids are deterministic, so re-running with the same arguments writes nothing.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from cashflow.db.repositories import upsert_entity, upsert_transactions
from cashflow.db.session import make_engine, make_session_factory, session_scope
from cashflow.demo.generator import DEFAULT_MONTHS, generate
from cashflow.settings import get_settings


@dataclass(frozen=True)
class SeededEntity:
    entity_id: str
    name: str
    transactions: int
    written: int


def seed(
    session: Session, *, entities: int, base_seed: int, as_of: date, months: int = DEFAULT_MONTHS
) -> list[SeededEntity]:
    """Seed `entities` businesses using consecutive seeds. The caller commits."""
    results: list[SeededEntity] = []
    for n in range(entities):
        dataset = generate(base_seed + n, as_of, months)
        upsert_entity(session, dataset.entity)
        written = upsert_transactions(session, dataset.transactions)
        results.append(
            SeededEntity(dataset.entity.id, dataset.entity.name, len(dataset.transactions), written)
        )
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--entities", type=int, default=2, help="number of businesses to seed")
    parser.add_argument("--seed", type=int, default=42, help="seed for the first business")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS, help="months of history")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=datetime.now(UTC).date(),
        help="last day of generated history (default: today, UTC)",
    )
    args = parser.parse_args(argv)

    engine = make_engine(get_settings())
    try:
        with session_scope(make_session_factory(engine)) as session:
            results = seed(
                session,
                entities=args.entities,
                base_seed=args.seed,
                as_of=args.as_of,
                months=args.months,
            )
    finally:
        engine.dispose()

    for r in results:
        print(f"{r.entity_id}  {r.name:<28} {r.transactions:>5} transactions, {r.written} written")


if __name__ == "__main__":
    main()
