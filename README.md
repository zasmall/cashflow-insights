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
curl "localhost:8000/entities/demo-42/summary"
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

## Ask Claude about the data (MCP)

The MCP server gives Claude read-only tools over the same data as the API. It needs Postgres running and a `.env` with `DATABASE_URL`.

**Claude Code** (run from anywhere; `--directory` points uv at this project):

```bash
claude mcp add cashflow-insights -- uv run --directory /absolute/path/to/cashflow-insights python -m cashflow.mcp_server
```

**Claude Desktop:** add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart. If Desktop can't find `uv`, use its absolute path (`which uv`).

```json
{
  "mcpServers": {
    "cashflow-insights": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/cashflow-insights", "python", "-m", "cashflow.mcp_server"]
    }
  }
}
```

Then ask things like:

- "Which businesses can you see, and how did each do last week?"
- "Will demo-42 run low on cash in the next 90 days? How confident is that?"
- "Explain the anomalies for demo-43 and show the transactions behind each."
- "Where did demo-42's money go in September?"
