# Cashflow Insights

Ingests categorized bank transactions, forecasts cash flow 30/60/90 days out with confidence bands, and flags anomalies. A FastAPI app (webhook ingest + reports) and an MCP server (so Claude can query forecasts) sit over one shared core.

> Work in progress. See [docs/ROADMAP.md](docs/ROADMAP.md) for status and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env
uv sync
docker compose up -d
uv run alembic upgrade head
```

Checks (all run in CI):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests marked `db` need Postgres; they skip locally when it isn't running and fail in CI.
