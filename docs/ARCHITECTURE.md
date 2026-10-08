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
| `entities` | id, name, currency, opening_balance, opening_balance_on, dirty_since | One client business. `id` is the upstream id; `currency` is ISO 4217; `dirty_since` flags a pending recompute |
| `inbound_events` | relay_event_id (unique), type, payload (JSONB), status, entity_id, error, received_at, processed_at | Dedupe log and outcome. `entity_id` has no FK so events for unprovisioned entities can be kept |
| `transactions` | id, entity_id, source_id, account_id, posted_on, amount, vendor, category, description, source_updated_at | Upsert on unique `(entity_id, source_id)`; never replaced by an older version |
| `recurring_series` | id, entity_id, vendor, typical_amount, cadence, anchor_day, next_expected_on, last_seen_on | Detected recurring bills/income; recomputed per entity, so no natural key. `anchor_day` is null for weekly cadences |
| `forecasts` | id, entity_id, generated_at, horizon_days, as_of, starting_balance, model, backtest_mase, backtest_coverage, backtest_balance_error | One row per horizon from one fitted model; only the latest run is kept |
| `forecast_points` | (forecast_id, on_date), expected_balance, lower, upper | Daily points; CHECK `lower <= expected <= upper` |
| `anomalies` | id, entity_id, type, severity, explanation, transaction_ids (array), fingerprint, detected_at, status, dismissed_at | Unique `(entity_id, fingerprint)`. Status: open, dismissed (by a person), resolved (by a rescan) |

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

**Recompute trigger:** when a processed event actually changes a transaction, ingest sets `entities.dirty_since` in the same transaction. After the response, a FastAPI `BackgroundTasks` job claims the entity atomically (`UPDATE ... SET dirty_since = NULL WHERE dirty_since IS NOT NULL RETURNING`) and refreshes it: recurring detection, then the forecast, and from M5 the anomaly scan. The claim and the refresh share one transaction:

- **Bursts:** of many events for one entity, one task wins the claim and the rest do nothing. An event that lands mid-refresh re-marks the entity for the next task.
- **Failures:** if a refresh fails, the claim rolls back too, so the entity stays dirty.
- **Restarts:** background tasks are in-process and die with the server, but the flag survives. `python -m cashflow.refresh --dirty`, run from cron, is the safety net. A durable job queue is the scaling path; see the README.

## Forecasting

`core.forecast.build_forecast` is pure: it takes an entity's history and returns a run with one fitted model and a forecast for each configured horizon (30/60/90 days). `refresh_entity` stores it. The pipeline at any cutoff date:

1. **Known flows:** detect recurring series (below) in the history up to the cutoff and project them over the horizon on their cadence and anchor day, at `typical_amount`. Overdue series resume from their next scheduled date, so the forecast assumes bills continue, the cautious choice for cash planning; M5 flags the miss. Known flows are treated as certain. A recent price change is projected at the median (old) amount until it becomes the norm; M5 flags it.
2. **Residual flows:** every transaction from a (vendor, direction) without a series, summed per day with zeros for quiet days, forecast by a statsforecast model with a prediction interval.
3. **Balance:** starting balance (opening balance plus all transactions to date) plus cumulative (known + residual mean). For the band, the model's one-step interval gives a daily standard deviation, and days are treated as independent and equally uncertain, so the band grows with √days.

**Why one-step spread:** later model intervals already accumulate uncertainty, so adding them up counts it twice. In backtests that gave about 93% coverage for an 80% band; the one-step approach gives about 75–84%.

**Backtest (honest, end to end):** the identical pipeline runs at past cutoffs (3 windows, 30 days apart, each needing 180 days of prior history), using only data before each cutoff, recurring detection included. Each run is scored against what happened:

| Metric | Meaning |
|---|---|
| `backtest_balance_error` | Mean absolute gap between forecast and actual balance over the horizon, in currency |
| `backtest_mase` | Daily net-flow error divided by a same-weekday-last-week guess; below 1 beats it |
| `backtest_coverage` | Share of days the actual balance stayed inside the band |

**Model choice:** each entity uses the candidate with the lowest balance error, not the lowest MASE. Daily-flow error rewards copying last week (SeasonalNaive), and those errors compound in the balance: on the demo data, SeasonalNaive sometimes won on MASE but had a 39% median balance error at 90 days, against about 6% for AutoETS and HistoricAverage. Defaults are AutoETS(weekly) and HistoricAverage. Too little history to backtest means HistoricAverage and null metrics.

**Measured accuracy** (20 demo businesses, forecasting from 90 days before the end of the data and comparing with what happened):

| Horizon | Mean abs error vs a flat-balance guess | Actual inside 80% band |
|---|---|---|
| 30 days | 0.99× (no real gain: lumpy client revenue dominates) | 80% |
| 60 days | 0.98× | 90% |
| 90 days | **0.58×** | 95% |

The forecast earns its keep further out, where known bills, income, and trend add up and a flat guess can't see them. At 60 and 90 days the bands are conservative. The test suite pins these results: the forecast must be no worse than the flat guess at 60 days, and at most 0.75× at 90 days, with at least 70% coverage at every horizon.

### Recurring detection

`core.recurring.detect_recurring` is a pure function. Polars computes per-group statistics and a small Python step classifies each group. There is one candidate per (vendor, direction), so refunds don't mix with charges. A candidate is recurring when all four hold (settings in `RecurringSettings`):

| Rule | Default |
|---|---|
| **Stable amount:** this share of charges is within the tolerance of the median, which becomes `typical_amount` | 75% within 10% |
| **Regular timing:** the median gap picks the nearest cadence, then this share of gaps match its calendar step | 75% within ±3 days |
| **Enough evidence:** minimum charges per cadence | weekly 4, biweekly 3, monthly 3, quarterly 3, annual 2 |
| **Not ended:** expected charges missed since the last one | at most 3 |

Details that matter:

- **Annual needs only two charges**, or annual bills would take three years to appear. Because two charges is thin evidence, their amounts must be identical. Real subscriptions repeat exactly; two similar flights a year apart don't.
- **Price changes don't break a series.** The median keeps the old amount until the new one is the majority, so M5 can flag the change. With very short histories (under about 12 months), two raised charges can be more than 25% of the series, and it drops out until history builds.
- **Calendar cadences have an anchor day,** the day of month that explains the most charges. A month-end charge fits any later anchor, so a bill on the 31st, or an annual bill on Feb 29, is predicted back on its true day after short months.
- **Ended series are dropped** after `max_missed_cycles`, so a cancelled subscription doesn't raise missed-bill alerts forever. A recently missed bill stays, so M5 can flag it.
- `refresh_recurring` replaces an entity's stored series in one transaction. Series ids aren't stable across runs, so anomaly fingerprints use vendor and dates instead.

**Known limitations** (candidates for "what I'd do next"):

- Two subscriptions from one vendor (say $89.99 and $22.99 monthly) split the amounts, so neither is detected. That is a miss, not a false alarm. Clustering by amount would fix it but would also split a price change into two series, so it needs a merge step for consecutive clusters.
- Usage-priced bills (cloud hosting, utilities) are monthly but too variable to pass the amount rule. They fall into the residual forecast instead.
- A bill anchored on the 1st that sometimes posts on the last day of the previous month can make the anchor day wrong by a few days. The anomaly grace period absorbs this.

**Verification:** example tests; Hypothesis properties showing that jittered, noisy series of every cadence are recovered and that unstable amounts or irregular timing never are; and an exact match against the demo generator's ground truth across random seeds, dates, and history lengths.

## Anomaly rules

`core.anomalies.scan` runs five pure rules over an entity's transactions and its detected recurring series. `refresh_entity` runs it after recurring detection and the forecast. Every finding carries a type, a severity, a human-readable explanation, and the evidence transactions.

| Type | Rule | Evidence | Fingerprint key |
|---|---|---|---|
| `duplicate_charge` | Same vendor and amount, outflow ≥ $20, within 3 days. A third copy extends the cluster | the charges | vendor + first charge |
| `category_spike` | A category's **non-recurring** outflow for the last complete month, or the month so far, is ≥ 3.5 robust z-scores above the median of the previous 6 months | that month's charges | category + month |
| `new_vendor_large` | A vendor's first-ever outflow is ≥ $1,000, after the warm-up | the charge | vendor |
| `missed_recurring` | A recurring bill or income is past `next_expected_on` plus 5 days' grace | the last charge that did arrive | vendor + direction + due date |
| `recurring_amount_change` | A series' latest charges differ from `typical_amount` by more than 15% | the unbroken run of changed charges | vendor + direction + first changed charge |

**Shared rules:**

- **Lookback:** only findings with evidence in the last 90 days are reported. A first scan of two years of history surfaces what matters now, not every old oddity.
- **Warm-up:** nothing in an entity's first 90 days of history counts as a new vendor, because at the start every vendor is new.
- **Severity** comes from dollar impact, the same way for every rule: low < $250 ≤ medium < $2,500 ≤ high. Impact is the excess charges for a duplicate, spend above typical for a spike, the charge for a new vendor, the missed amounts for a missed bill, and the yearly difference for an amount change.
- **Outflows only,** except missed bills, where missing income matters too ("Initech's monthly payment ... hasn't arrived").

**Category-spike guards** (each added after measuring false positives on generated data):

- Recurring charges are excluded, since an annual bill landing is expected. Price changes have their own rule.
- The category must have spend in at least 4 of the 6 baseline months, so occasional categories (travel) don't spike with every trip.
- The MAD is at least 10% of the median, so steady categories aren't hair-trigger.
- **Materiality:** the month must be at least $500 above typical. An extra $300 of fuel can be statistically unusual and still not worth an alert.

**Idempotency:** `sync_anomalies` upserts on `(entity_id, fingerprint)`. Open anomalies get refreshed severity, explanation, and evidence, so a spike growing through the month updates in place. Dismissed and resolved anomalies are never touched, so a rescan can't reopen them.

**Resolution:** an open `missed_recurring` anomaly that a rescan no longer reports is marked `resolved` if the bill has since arrived (a later charge from that vendor in that direction). Only missed bills auto-resolve. For other rules, "no longer found" usually just means the evidence aged out of the lookback.

**Verification:** example tests per rule, covering boundaries, guards, and fingerprint stability. A Hypothesis property checks that a scan of generated data finds exactly the five planted anomalies, with their evidence, and nothing else; a one-off sweep of 4,000 random datasets found no exceptions.

## API

- `POST /webhooks/relay`: ingest
- `GET /entities/{id}/forecast?horizon=30|60|90`: latest stored forecast with daily points and backtest metrics (MASE, band coverage, typical balance error). 404 for unknown entities or before the first refresh, and 422 for an unconfigured horizon. Like every report endpoint, it is unauthenticated for now; API keys are on the "what I'd do next" list.
- `GET /entities/{id}/anomalies?status=open|dismissed|resolved`: most severe first, then most recent. 404 for unknown entities.
- `PATCH /entities/{id}/anomalies/{anomaly_id}` with `{"status": "dismissed"}` or `{"status": "open"}` to undo. Entity-scoped: another entity's anomaly id returns 404. `resolved` is set only by rescans; changing a resolved anomaly returns 409.
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
- usage-priced cloud hosting, monthly but alternating quiet and busy months, so it can never pass the recurring amount rule
- variable spend with weekday/weekend and start-of-month seasonality
- one planted instance of each anomaly type: a duplicate charge, a price increase, a missed bill, a category spike, and a large first charge from a new vendor

It also returns **ground truth**: the recurring series it planted and the source ids behind each planted anomaly. Accidental anomalies are prevented by construction, so detection tests can assert exact recovery:

- Random charges never repeat a vendor and amount within 14 days (no accidental duplicates).
- Each discretionary category's month stays within 1.75× its trailing six-month median (no accidental spikes). Office supplies get a small guaranteed monthly restock, so the category always has a baseline for the planted spike.
- The planted spike rotates vendors, so it can't pass as a weekly recurring series.

Property tests check this across random seeds.

`python -m cashflow.demo.seed` loads consecutive seeds as separate entities (two by default) and is idempotent. Tests and the demo both use the generator, so no real financial data ever enters the repo.

## Non-goals

- Bank connections: the categorizer owns ingestion
- Multi-currency conversion: a single currency per entity
- Writing back to source systems
- Real-time streaming
