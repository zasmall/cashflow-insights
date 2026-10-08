"""Load synthetic demo entities into the database.

    uv run python -m cashflow.demo.seed [--entities 2] [--seed 42] [--as-of 2026-10-08]

Idempotent: generated ids are deterministic, so re-running with the same arguments writes no
transactions. Rows an older generator produced are pruned. Recurring series, forecasts, and
anomalies are recomputed each run.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from cashflow.core.refresh import refresh_entity
from cashflow.db.repositories import (
    delete_anomalies,
    prune_transactions,
    upsert_entity,
    upsert_transactions,
)
from cashflow.db.session import make_engine, make_session_factory, session_scope
from cashflow.demo.generator import DEFAULT_MONTHS, generate
from cashflow.settings import Settings, get_settings


@dataclass(frozen=True)
class SeededEntity:
    entity_id: str
    name: str
    transactions: int
    written: int
    pruned: int
    recurring_series: int
    forecast_model: str


def seed(
    session: Session,
    *,
    entities: int,
    base_seed: int,
    as_of: date,
    settings: Settings,
    months: int = DEFAULT_MONTHS,
) -> list[SeededEntity]:
    """Seed `entities` businesses using consecutive seeds. The caller commits."""
    results: list[SeededEntity] = []
    for n in range(entities):
        dataset = generate(base_seed + n, as_of, months)
        upsert_entity(session, dataset.entity)
        written = upsert_transactions(
            session,
            dataset.transactions,
            # The seeder is the source here, emitting this version now. Using now (not as_of)
            # lets a changed generator overwrite older rows; identical rows still write nothing.
            source_updated_at=datetime.now(UTC),
        )
        # The seeder owns these entities' whole history, so rows an older generator produced
        # are stale. Their anomalies may cite deleted rows; the refresh below recreates them.
        pruned = prune_transactions(
            session, dataset.entity.id, {t.source_id for t in dataset.transactions}
        )
        if pruned:
            delete_anomalies(session, dataset.entity.id)
        refreshed = refresh_entity(session, dataset.entity.id, as_of=as_of, settings=settings)
        results.append(
            SeededEntity(
                dataset.entity.id,
                dataset.entity.name,
                len(dataset.transactions),
                written,
                pruned,
                len(refreshed.series),
                refreshed.forecast.model.value,
            )
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

    settings = get_settings()
    engine = make_engine(settings)
    try:
        with session_scope(make_session_factory(engine)) as session:
            results = seed(
                session,
                entities=args.entities,
                base_seed=args.seed,
                as_of=args.as_of,
                settings=settings,
                months=args.months,
            )
    finally:
        engine.dispose()

    for r in results:
        print(
            f"{r.entity_id}  {r.name:<28} {r.transactions:>5} transactions "
            f"({r.written} written, {r.pruned} pruned), "
            f"{r.recurring_series} recurring series, forecast with {r.forecast_model}"
        )


if __name__ == "__main__":
    main()
