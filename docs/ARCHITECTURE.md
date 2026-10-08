# Architecture

## Purpose

Turn a stream of categorized transactions into forward-looking insight a small business owner can act on: where cash will be in 30, 60, and 90 days, and what looks wrong right now.

## System context

```
Transaction Categorizer ──(transaction.categorized)──▶ Webhook Relay ──▶ Cashflow Insights API
                                                                              │
                                                                         core services ◀── MCP server ◀── Claude
```

## Inbound contract

Event type `transaction.categorized`. The payload must be agreed with the categorizer:

```json
{
  "entity_id": "17",
  "transaction": {
    "id": "48213",
    "account_id": "3",
    "posted_on": "2026-10-01",
    "amount": "-129.99",
    "currency": "USD",
    "description": "ADOBE *CREATIVE CLD",
    "vendor": "Adobe",
    "category": "Software & Subscriptions"
  }
}
```

Negative amounts are outflows. Amounts are decimal strings and never floats.

**Identifiers are opaque strings** (up to 64 chars). The categorizer currently uses auto-increment integers (`entity_id` is its `client_id`, `account_id` its `bank_account_id`), but nothing here parses or assumes a format, so its ID scheme can change without breaking ingest. The field mapping is finalized in M8.

## Data model

| Table | Key columns | Notes |
|---|---|---|
| `entities` | id, name, currency, opening_balance, opening_balance_on | One client business. `id` is the upstream id; `currency` is ISO 4217 |
| `inbound_events` | relay_event_id (unique), type, payload (JSONB), received_at, processed_at | Dedupe log; raw payload kept for replay and debugging |
| `transactions` | id, entity_id, source_id, account_id, posted_on, amount, vendor, category, description | Upsert on unique `(entity_id, source_id)` |
| `recurring_series` | id, entity_id, vendor, typical_amount, cadence, next_expected_on, last_seen_on | Detected recurring bills/income; recomputed per entity, so no natural key |
| `forecasts` | id, entity_id, generated_at, horizon_days, model, backtest_mase | Header row per run |
| `forecast_points` | (forecast_id, on_date), expected_balance, lower, upper | Daily points; CHECK `lower <= expected <= upper` |
| `anomalies` | id, entity_id, type, severity, explanation, transaction_ids (array), fingerprint, detected_at, status, dismissed_at | Unique `(entity_id, fingerprint)` |

Conventions:

- **Keys:** upstream identifiers (`entities.id`, `source_id`, `account_id`) are `VARCHAR(64)`. This service's own rows use `BIGINT` identity keys.
- **Types:** money is `NUMERIC(14,2)`; timestamps are `TIMESTAMPTZ`; business dates are `DATE`.
- **Enums** (`cadence`, anomaly `type`/`severity`/`status`) are `VARCHAR` with a CHECK constraint rather than native Postgres enums, which are awkward to alter in migrations. The Python `StrEnum`s live in `core.enums`.
- **Scoping:** every entity-owned table has an `entity_id` foreign key with `ON DELETE CASCADE`.
- **Anomaly idempotency:** each rule computes a `fingerprint` (a hash of the type and its evidence). The unique constraint means a rescan can neither duplicate an open anomaly nor resurrect a dismissed one.

## Ingest flow

1. Read the raw body and verify the signature, rejecting with 401 on failure.
2. Insert into `inbound_events`. A duplicate `relay_event_id` returns 200 without reprocessing.
3. Validate the payload with Pydantic and upsert the transaction.
4. Mark the entity dirty, so recurring detection, the forecast, and the anomaly scan re-run. Start with FastAPI `BackgroundTasks`; this is noted as a scaling limit in the README.

## Forecasting

1. **Recurring detection:** group by vendor, then find series with a stable cadence (weekly, biweekly, monthly, quarterly, annual) and stable amounts within a tolerance. This step is deterministic and property-tested.
2. **Known flows:** project recurring series forward on their cadence.
3. **Residual flows:** aggregate the remaining non-recurring net flow per day and forecast it with statsforecast (for example AutoETS). Prediction intervals give the bands.
4. **Balance:** take the opening/current balance plus cumulative (recurring + residual), with the residual bands carried through.
5. **Backtest:** use a rolling-origin evaluation and store the MASE on each forecast run. Report it in the API and README, since an honest accuracy number is a senior signal.

## Anomaly rules

Each rule is a pure function from transactions and context to a list of anomalies.

| Type | Rule |
|---|---|
| `duplicate_charge` | Same vendor and amount within N days |
| `category_spike` | Category spend this period exceeds a robust z-score (median/MAD) vs. a trailing baseline |
| `new_vendor_large` | First-ever charge from a vendor above a threshold |
| `missed_recurring` | A recurring series is past `next_expected_on` plus a grace period |
| `recurring_amount_change` | A recurring charge deviates beyond tolerance from `typical_amount` |

Rules are idempotent, so re-running a scan never duplicates open anomalies.

## API

- `POST /webhooks/relay`: ingest
- `GET /entities/{id}/forecast?horizon=30|60|90`
- `GET /entities/{id}/anomalies?status=open`
- `PATCH /entities/{id}/anomalies/{anomaly_id}`: dismiss
- `GET /entities/{id}/summary`: weekly summary (balance outlook, top categories, open anomalies)

## MCP server

FastMCP with stdio transport, for Claude Desktop and Claude Code. All tools are read-only, entity-scoped, and return Pydantic models.

| Tool | Purpose |
|---|---|
| `list_entities` | Discover what's available |
| `get_forecast(entity_id, horizon_days)` | Balance outlook with bands and backtest accuracy |
| `list_anomalies(entity_id, status)` | Current flags |
| `explain_anomaly(entity_id, anomaly_id)` | Explanation plus evidence transactions |
| `spend_by_category(entity_id, start, end)` | Category totals |
| `list_recurring(entity_id)` | Detected subscriptions and bills |

## Demo data

The synthetic generator is seeded and deterministic. It produces:
- payroll and rent
- several subscriptions
- variable spend with weekly and monthly seasonality
- injected anomalies: a duplicate charge, a price increase, a missed bill, and a spike

Tests and the demo both use it, so no real financial data ever enters the repo.

## Non-goals

- Bank connections: the categorizer owns ingestion
- Multi-currency conversion: a single currency per entity
- Writing back to source systems
- Real-time streaming
