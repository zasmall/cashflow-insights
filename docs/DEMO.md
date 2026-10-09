# End-to-end demo

Transactions approved in **Transaction Categorizer** flow through **Webhook Relay** into **Cashflow Insights**, which detects recurring bills, forecasts the balance, and scans for anomalies. `scripts/e2e-demo.sh` automates the steps below; this page explains what each one does and what you should see.

```
Transaction Categorizer ──POST /api/events──▶ Webhook Relay ──signed POST /webhooks/relay──▶ Cashflow Insights
   (approve a categorization)                  (dedupe, retry, rate-limit)                   (ingest, refresh)
```

## Prerequisites

The three repos side by side (`~/Sites/learning/` here), each set up per its own README: MySQL and Redis for the relay, Redis for the categorizer, and Docker's Postgres for this app (`docker compose up -d`). The categorizer needs its publishing support, on `main` since [zasmall/Transaction-categorizer#1](https://github.com/zasmall/Transaction-categorizer/pull/1).

| App | Port | Start | Queue worker |
|---|---|---|---|
| Webhook Relay | 8000 | `php artisan serve --port=8000` | `php artisan horizon` (delivers webhooks) |
| Transaction Categorizer | 8002 (UI only) | `php artisan serve --port=8002` | `php artisan horizon` (publishes events) |
| Cashflow Insights | 8003 | `uv run fastapi dev src/cashflow/api/main.py --port 8003` | none (refreshes in background tasks) |

The relay's `.env` must allow localhost endpoints (`RELAY_ALLOW_PRIVATE_NETWORKS=true`, the default in its `.env.example`).

## 1. Setup (once)

```bash
scripts/e2e-demo.sh setup
```

1. Registers the categorizer as a relay **source** (`relay:source:create transaction-categorizer`), which returns a source token.
2. Creates a relay **endpoint** for `http://localhost:8003/webhooks/relay`, subscribed to `transaction.categorized`, through the relay's API. The response carries the endpoint's signing secret.
3. Writes `WEBHOOK__SECRET` into this repo's `.env`, and `RELAY_URL`, `RELAY_SOURCE_TOKEN`, and `RELAY_CURRENCY` into the categorizer's.
4. Records what it did in `.e2e/state` (gitignored). Delete that file, and the source and endpoint in the relay, to start over.

Then **restart Cashflow Insights' API and the categorizer's Horizon**, so they read the new settings.

## 2. Run

```bash
scripts/e2e-demo.sh run
```

1. **Provision** Northwind Coffee Co. (the categorizer's client 1) here, with a demo opening balance of $25,000 on 2025-12-31. Ingest never creates entities: the opening balance has to come from a person. Events that arrive before provisioning are stored as `unknown_entity`, and provisioning applies them.
2. **Backfill**: `transactions:publish-categorized northwind-coffee-co` queues one `transaction.categorized` event per approved transaction (95). Each event's idempotency key is its categorization id.
3. **Wait** while the relay delivers them. Expect a pause after 60: the relay rate-limits each endpoint to 60 deliveries a minute (`RELAY_RATE_LIMIT_PER_MINUTE`) so a burst can't flatten a receiver. Every delivery is verified, deduplicated, and stored, and the entity is refreshed in the background.
4. **Refresh as of 2026-03-31.** The categorizer's demo data covers January to March 2026, so viewing it "as of today" would show a forecast from a stale balance and no recent anomalies.
5. **Print** the weekly summary for 23–29 March: cash in and out, the 90-day outlook and its low point, and open anomalies.

Running `run` again is safe and shows the system is idempotent end to end: the backfill re-sends the same 95 events, the relay recognizes every idempotency key, nothing is delivered, and Cashflow Insights still has exactly 95 events.

## What you should see

```
==> Waiting for 95 transactions to arrive through the relay
    95/95 received, refresh pending: no
==> Weekly summary for the week of 2026-03-23
    Northwind Coffee Co., 2026-03-23 to 2026-03-29
    in 10402.96, out 852.16; balance 116461.28 -> 126012.08
    90-day outlook: 237074.28 (80% band 224005.65 to 250142.91)
    lowest expected balance: 128155.33 on 2026-04-01
    open anomalies: {'high': 0, 'medium': 0, 'low': 0}
```

Then explore:

- `http://localhost:8003/docs`: the API, interactively (`/entities/1/forecast`, `/entities/1/summary?week_of=2026-03-23`, ...).
- The relay's dashboard at `http://localhost:8000` (`demo@example.com` / `password`): each delivery, its attempts, and the 200 responses.
- Claude, through the MCP server (see the README): "What does Northwind Coffee Co.'s cash look like going into April?"
- Live updates: approve a transaction in the categorizer (`http://localhost:8002`). Within a few seconds it arrives here and the entity refreshes.

## Caveats worth knowing

- **Only approved decisions are published.** AI suggestions and uncategorized transactions stay in the categorizer until a person (or a rule) approves them. In the fresh demo data, Northwind has about 60 outflows (roughly $13k) and 3 inflows ($6k) still awaiting review, so Cashflow Insights' balance runs about $7k high. Approving them in the categorizer's review queue sends them through. A future `transaction.imported` event (before categorization) would close this gap.
- **The demo data is small:** three months of one coffee shop. Recurring detection finds one series, the forecast falls back to HistoricAverage (too little history to backtest), and none of the five anomaly rules fire. The synthetic businesses from `python -m cashflow.demo.seed` show those features in full.
