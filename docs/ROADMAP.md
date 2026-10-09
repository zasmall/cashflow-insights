# Roadmap

## M0 — Scaffold
- [x] `uv init` with `src/cashflow` package layout, Python 3.13
- [x] Ruff, mypy (strict), pytest + Hypothesis configured in `pyproject.toml`
- [x] Docker Compose for Postgres; pydantic-settings config
- [x] SQLAlchemy 2.0 session factory + Alembic
- [x] GitHub Actions: Ruff, mypy, pytest (with a Postgres service)

## M1 — Data model & demo data
- [x] Models + migrations for all tables in ARCHITECTURE.md
- [x] Deterministic synthetic generator with injected anomalies
- [x] Seeder command

## M2 — Webhook ingest
- [x] Signature verification (pure function, Hypothesis-tested)
- [x] `POST /webhooks/relay` with dedupe on relay event id
- [x] Transaction upsert; tests for bad signature, stale timestamp, duplicate delivery, invalid payload

## M3 — Recurring detection
- [x] Cadence and amount-stability detection per vendor
- [x] Property tests: generated series are recovered; noise isn't mistaken for a series

## M4 — Forecasting
- [x] Recurring projection + statsforecast residual model
- [x] Balance series with confidence bands
- [x] Rolling-origin backtest; MASE stored per run
- [x] `GET /entities/{id}/forecast`

## M5 — Anomaly detection
- [x] All five rules as pure functions, each with tests
- [x] Idempotent scan; anomaly endpoints (list, dismiss)

## M6 — Summary
- [x] `GET /entities/{id}/summary` weekly report

## M7 — MCP server
- [x] MCPServer (the SDK's v2 name for FastMCP) with the six read-only tools, plus `get_weekly_summary`
- [x] Tool tests over the MCP protocol (in-process and stdio); entity-scoping tests
- [x] README section on connecting it to Claude Desktop / Claude Code

## M8 — Integration
- [x] Register as an endpoint on the Webhook Relay; categorizer emits `transaction.categorized` (categorizer branch `feat/publish-categorized`)
- [x] End-to-end demo across the three repos (`scripts/e2e-demo.sh` + `docs/DEMO.md`)
- [x] Entity provisioning CLI, plus reprocessing of stored `unknown_entity` events
- [x] Version transactions by the source's `categorized_at`, not the relay's receive time

## M9 — Portfolio polish
- [x] README: problem, system diagram, forecasting approach and backtest accuracy, key decisions, how to run, "what I'd do next"
- [x] Forecast chart (`scripts/forecast_chart.py`, light and dark) and a real Claude session over the MCP tools as a text transcript (`docs/EXAMPLE_SESSION.md`) instead of a clip
- [x] AGPL-3.0-or-later license
