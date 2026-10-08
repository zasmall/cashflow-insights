"""Recompute recurring series and forecasts.

    uv run python -m cashflow.refresh --dirty          # entities flagged by ingest (cron-safe)
    uv run python -m cashflow.refresh --entity demo-42
    uv run python -m cashflow.refresh --all
    uv run python -m cashflow.refresh --entity 1 --as-of 2026-03-31   # view older data as of a date

Ingest refreshes in a background task after each response. That task is in-process, so this
command is the safety net: entities stay flagged until a refresh succeeds.
"""

import argparse
from datetime import UTC, date, datetime

from cashflow.core.refresh import refresh_entity, refresh_if_dirty
from cashflow.db import repositories
from cashflow.db.session import make_engine, make_session_factory, session_scope
from cashflow.settings import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--dirty", action="store_true", help="entities with new transactions")
    target.add_argument("--all", action="store_true", help="every entity")
    target.add_argument("--entity", help="one entity id")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=datetime.now(UTC).date(),
        help="date to forecast from and scan up to (default: today, UTC); useful for old data",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    as_of = args.as_of
    try:
        if args.dirty:
            with factory() as session:
                ids = repositories.dirty_entity_ids(session)
            for entity_id in ids:
                result = refresh_if_dirty(factory, entity_id, as_of=as_of, settings=settings)
                print(f"{entity_id}: {'refreshed' if result else 'claimed elsewhere'}")
            print(f"{len(ids)} dirty entities")
            return

        with factory() as session:
            ids = repositories.entity_ids(session) if args.all else [args.entity]
        for entity_id in ids:
            with session_scope(factory) as session:
                result = refresh_entity(session, entity_id, as_of=as_of, settings=settings)
            print(
                f"{entity_id}: {len(result.series)} recurring series, "
                f"forecast with {result.forecast.model}"
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
