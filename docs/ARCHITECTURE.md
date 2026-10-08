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
  "entity_id": "01J...",
  "transaction": {
    "id": "01J...",
    "account_id": "01J...",
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

## Data model

| Table | Key columns | Notes |
|---|---|---|
| `entities` | id, name, opening_balance, opening_balance_on | One client business |
| `inbound_events` | relay_event_id (unique), type, received_at, processed_at | Dedupe log |
| `transactions` | id, entity_id, account_id, posted_on, amount, vendor, category, description | Upsert by source transaction id |
| `recurring_series` | id, entity_id, vendor, typical_amount, cadence, next_expected_on, last_seen_on | Detected recurring bills/income |
| `forecasts` | id, entity_id, generated_at, horizon_days, model, backtest_mase | Header row per run |
| `forecast_points` | forecast_id, on_date, expected_balance, lower, upper | Daily points |
| `anomalies` | id, entity_id, type, severity, explanation, transaction_ids (array), detected_at, status | Status: open, dismissed |

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
