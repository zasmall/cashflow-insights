"""Provision and list entities (client businesses).

    uv run python -m cashflow.entities add --id 17 --name "Acme Ltd" \\
        --opening-balance 25000.00 --as-of 2026-01-01 [--currency USD]
    uv run python -m cashflow.entities list

`add` also applies any events that arrived before the entity existed (stored as
`unknown_entity`), then refreshes its recurring series, forecast, and anomalies.
"""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal

from cashflow.core import queries
from cashflow.core.entities import provision_entity
from cashflow.core.models import Entity
from cashflow.db.session import make_engine, make_session_factory, session_scope
from cashflow.settings import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="create or update an entity")
    add.add_argument("--id", required=True, help="the categorizer's client id")
    add.add_argument("--name", required=True)
    add.add_argument("--currency", default="USD")
    add.add_argument("--opening-balance", type=Decimal, required=True)
    add.add_argument(
        "--as-of", type=date.fromisoformat, required=True, help="date of the opening balance"
    )
    commands.add_parser("list", help="list entities")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = make_engine(settings)
    try:
        with session_scope(make_session_factory(engine)) as session:
            if args.command == "list":
                for e in queries.list_entities(session):
                    print(
                        f"{e.id:<12} {e.name:<30} {e.currency}  last txn {e.last_transaction_on}"
                        f"  open anomalies {e.open_anomalies}"
                    )
                return
            entity = Entity(
                id=args.id,
                name=args.name,
                currency=args.currency,
                opening_balance=args.opening_balance,
                opening_balance_on=args.as_of,
            )
            result = provision_entity(
                session, entity, as_of=datetime.now(UTC).date(), settings=settings
            )
        waiting = ", ".join(f"{n} {s.value}" for s, n in result.reprocessed.items()) or "none"
        print(
            f"{entity.id}: provisioned. Waiting events applied: {waiting}. "
            f"{len(result.refresh.series)} recurring series, "
            f"forecast with {result.refresh.forecast.model.value}."
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
