# Cashflow Insights

Ingests categorized bank transactions, forecasts cash flow 30/60/90 days out with confidence bands, and flags anomalies. A FastAPI app (webhook ingest + reports) and an MCP server (so Claude can query forecasts) sit over one shared core.

> Work in progress. See [docs/ROADMAP.md](docs/ROADMAP.md) for status and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env                  # then set WEBHOOK__SECRET (the API won't start without it)
uv sync
docker compose up -d
uv run alembic upgrade head
uv run python -m cashflow.demo.seed   # two synthetic businesses, 24 months each, with forecasts
uv run fastapi dev src/cashflow/api/main.py
curl "localhost:8000/entities/demo-42/forecast?horizon=90"
curl "localhost:8000/entities/demo-42/anomalies"
```

New transactions trigger a background refresh of the entity's recurring series, forecast, and anomaly scan. Background tasks are in-process, so a restart can drop one. The entity stays flagged, though, and this command, run from cron, catches up:

```bash
uv run python -m cashflow.refresh --dirty    # or --entity ID, or --all
```

Checks (all run in CI):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests marked `db` run against a separate `cashflow_test` database, recreated and migrated on each run, so they never touch dev data. They skip locally when Postgres isn't running and fail in CI.
