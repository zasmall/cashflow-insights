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
- [ ] `GET /entities/{id}/summary` weekly report

## M7 — MCP server
- [ ] FastMCP server with the six read-only tools
- [ ] Tool tests calling tools directly; entity-scoping tests
- [ ] README section on connecting it to Claude Desktop / Claude Code

## M8 — Integration
- [ ] Register as an endpoint on the Webhook Relay; categorizer emits `transaction.categorized`
- [ ] End-to-end demo across the three repos (docker compose or script)
- [ ] Entity provisioning CLI, plus reprocessing of stored `unknown_entity` events

## M9 — Portfolio polish
- [ ] README: problem, system diagram, forecasting approach and backtest accuracy, key decisions, how to run, "what I'd do next"
- [ ] Forecast chart screenshot and a short clip of Claude using the MCP tools
