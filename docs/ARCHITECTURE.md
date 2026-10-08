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

The relay POSTs a JSON envelope around the source system's payload:

```json
{
  "id": "evt_...",
  "type": "transaction.categorized",
  "created_at": "2026-10-01T14:03:22Z",
  "data": { ... }
}
```

Headers: `X-Relay-Signature` (below), plus `X-Relay-Event-Id`, `X-Relay-Event-Type` and `X-Relay-Delivery-Id`. Those three headers are informational only; we use the body's `id` and `type`, because the body is signed and they are not.

**Signature:** `t=<unix>,v1=<hex HMAC-SHA256 of "{t}.{raw_body}">`, with one `v1` per active secret while the relay rotates secrets. We accept the request if any `v1` matches, ignore unknown schemes such as `v2`, and reject timestamps outside the tolerance window (300 s by default). A test vector produced by the relay's own PHP signer pins compatibility.

For `transaction.categorized`, `data` must be agreed with the categorizer:

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
| `inbound_events` | relay_event_id (unique), type, payload (JSONB), status, entity_id, error, received_at, processed_at | Dedupe log and outcome. `entity_id` has no FK so events for unprovisioned entities can be kept |
| `transactions` | id, entity_id, source_id, account_id, posted_on, amount, vendor, category, description, source_updated_at | Upsert on unique `(entity_id, source_id)`; never replaced by an older version |
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

`POST /webhooks/relay`:

1. Read the raw body, capped at `WEBHOOK__MAX_BODY_BYTES`, and verify the signature *before* parsing anything.
2. Parse the envelope.
3. In **one database transaction**:
   1. Insert into `inbound_events` (`ON CONFLICT DO NOTHING`). If the event was already received, answer with its original status and `duplicate: true`.
   2. Decide the outcome:
      - unsupported type → `ignored`
      - payload fails validation → `invalid`
      - entity not provisioned → `unknown_entity`
      - currency differs from the entity's → `invalid`
      - otherwise upsert the transaction → `processed`
   3. Record the status (and the error, for `invalid`), then commit before responding.

Because recording and applying commit together, a crash mid-ingest leaves no dedupe record, so the relay's retry is processed normally instead of being dropped as a duplicate. Concurrent deliveries of the same event serialize on the unique index.

**Out-of-order delivery:** retries mean an older event can arrive after a newer one. Each transaction keeps `source_updated_at`, taken from the envelope's signed `created_at`, and the upsert only replaces a row with a version at least as new. A delayed retry therefore can't undo a later recategorization.

**Responses** follow the relay's retry policy, where any 2xx is final, 410 disables the endpoint, and every other status is retried with backoff and counts toward its circuit breaker:

| Case | Status |
|---|---|
| processed, duplicate, ignored, invalid, unknown entity | 200 `{"status": ..., "duplicate": bool}` |
| missing, malformed, stale, or mismatched signature | 401 |
| signed body that isn't a relay envelope | 400 |
| body over the size limit | 413 |
| unexpected server error (nothing committed) | 500 |

Problems that a retry can't fix answer 200 and are recorded with a reason, so one bad event can't trip the relay's breaker and pause every delivery. We never send 410.

**Unknown entities:** entities are provisioned here (for now, by the seeder) rather than created from events. Events for an unknown entity are stored as `unknown_entity` with their payload, ready to be reprocessed once it exists.

**Recompute trigger:** marking the entity dirty, so recurring detection, the forecast, and the anomaly scan re-run, arrives with M4/M5, when there is something to recompute. It starts with FastAPI `BackgroundTasks`, noted as a scaling limit in the README.

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

`cashflow.demo.generator.generate(seed, as_of, months=24)` is a pure function: the same arguments always give the same dataset. It produces one business with:
- client revenue plus a monthly retainer, biweekly payroll, and rent
- monthly, quarterly, and annual subscriptions and bills
- usage-priced cloud hosting (monthly but deliberately too variable to count as recurring)
- variable spend with weekday/weekend and start-of-month seasonality
- one planted instance of each anomaly type: a duplicate charge, a price increase, a missed bill, a category spike, and a large first charge from a new vendor

It also returns **ground truth**: the recurring series it planted and the source ids behind each planted anomaly. Accidental anomalies are prevented by construction; for example, random charges never repeat a vendor and amount within 14 days. Property tests check this across random seeds, so detection tests can assert exact recovery.

`python -m cashflow.demo.seed` loads consecutive seeds as separate entities (two by default) and is idempotent. Tests and the demo both use the generator, so no real financial data ever enters the repo.

## Non-goals

- Bank connections: the categorizer owns ingestion
- Multi-currency conversion: a single currency per entity
- Writing back to source systems
- Real-time streaming
