#!/usr/bin/env bash
# End-to-end demo: Transaction Categorizer -> Webhook Relay -> Cashflow Insights.
#
#   scripts/e2e-demo.sh setup   # once: register the relay source and endpoint, write secrets
#   scripts/e2e-demo.sh run     # provision the demo client here, backfill, and show results
#
# See docs/DEMO.md for what to start first and what you should see.
# Sibling repos default to ../webhook-relay-service and ../transaction-categorizer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELAY_DIR="${RELAY_DIR:-$ROOT/../webhook-relay-service}"
CATEGORIZER_DIR="${CATEGORIZER_DIR:-$ROOT/../transaction-categorizer}"
RELAY_URL="${RELAY_URL:-http://localhost:8000}"
CASHFLOW_URL="${CASHFLOW_URL:-http://localhost:8003}"
RELAY_USER="${RELAY_USER:-demo@example.com}"
STATE="$ROOT/.e2e/state"

# The categorizer's richest demo client, and the window its demo data covers.
CLIENT_SLUG="northwind-coffee-co"
ENTITY_ID="1"
ENTITY_NAME="Northwind Coffee Co."
OPENING_BALANCE="25000.00"   # demo value: the categorizer doesn't know balances
OPENING_ON="2025-12-31"
DATA_END="2026-03-31"
SUMMARY_WEEK="2026-03-23"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_up() {  # name url
  curl -sf -o /dev/null --max-time 5 "$2" || die "$1 isn't answering at $2. See docs/DEMO.md for how to start it."
}

require_horizon() {  # name dir
  (cd "$2" && php artisan horizon:status 2>/dev/null) | grep -qi running \
    || die "$1's Horizon isn't running. Start it with: (cd $2 && php artisan horizon)"
}

set_env() {  # file key value: replace the key's line, or append it
  local file="$1" key="$2" value="$3" tmp
  tmp="$(mktemp)"
  { grep -v "^${key}=" "$file" 2>/dev/null || true; echo "${key}=${value}"; } > "$tmp"
  mv "$tmp" "$file"
}

json_field() {  # json path... : print a field, looking inside a Laravel resource's "data" wrapper
  python3 -c '
import json, sys
body = json.loads(sys.argv[1])
node = body.get("data", body)
for key in sys.argv[2:]:
    node = node[key]
print(node)' "$@"
}

setup() {
  [[ -f "$STATE" ]] && { echo "Already set up (state in $STATE). Delete it to start over."; return; }
  require_up "Webhook Relay" "$RELAY_URL/up"

  say "Registering the categorizer as a relay source"
  local source_token
  source_token="$(cd "$RELAY_DIR" && php artisan relay:source:create transaction-categorizer | tail -n 1)" \
    || die "couldn't create the source (it may already exist from an earlier setup)"

  say "Subscribing Cashflow Insights to transaction.categorized"
  local user_token endpoint
  user_token="$(cd "$RELAY_DIR" && php artisan relay:user:token "$RELAY_USER" --name=cashflow-e2e | tail -n 1)"
  endpoint="$(curl -sf -X POST "$RELAY_URL/api/endpoints" \
    -H "Authorization: Bearer $user_token" -H 'Accept: application/json' -H 'Content-Type: application/json' \
    -d "{\"url\": \"$CASHFLOW_URL/webhooks/relay\", \"description\": \"Cashflow Insights\", \"event_types\": [\"transaction.categorized\"]}")" \
    || die "couldn't create the endpoint"

  say "Writing secrets into each app's .env"
  set_env "$ROOT/.env" WEBHOOK__SECRET "$(json_field "$endpoint" secret)"
  set_env "$CATEGORIZER_DIR/.env" RELAY_URL "$RELAY_URL"
  set_env "$CATEGORIZER_DIR/.env" RELAY_SOURCE_TOKEN "$source_token"
  set_env "$CATEGORIZER_DIR/.env" RELAY_CURRENCY USD

  mkdir -p "$(dirname "$STATE")"
  printf 'ENDPOINT_ID=%s\nSOURCE_TOKEN=%s\n' "$(json_field "$endpoint" id)" "$source_token" > "$STATE"
  chmod 600 "$STATE"

  say "Done. Restart Cashflow Insights' API and the categorizer's Horizon so they read the new"
  echo "    settings, then run: scripts/e2e-demo.sh run"
}

entity_state() {  # prints "<transactions> <refresh pending>" for the demo entity
  (cd "$ROOT" && uv run --quiet python - "$ENTITY_ID" <<'PY'
import sys
from sqlalchemy import func, select
from cashflow.db import models as orm
from cashflow.db.session import make_engine, make_session_factory
from cashflow.settings import get_settings

engine = make_engine(get_settings())
with make_session_factory(engine)() as session:
    count = session.scalar(
        select(func.count()).select_from(orm.Transaction).where(orm.Transaction.entity_id == sys.argv[1])
    )
    entity = session.get(orm.Entity, sys.argv[1])
    print(count, "yes" if entity and entity.dirty_since else "no")
engine.dispose()
PY
  )
}

run() {
  [[ -f "$STATE" ]] || die "run 'scripts/e2e-demo.sh setup' first"
  require_up "Webhook Relay" "$RELAY_URL/up"
  require_up "Cashflow Insights API" "$CASHFLOW_URL/openapi.json"
  require_horizon "Webhook Relay" "$RELAY_DIR"
  require_horizon "Transaction Categorizer" "$CATEGORIZER_DIR"

  say "Provisioning $ENTITY_NAME (entity $ENTITY_ID) with its opening balance"
  (cd "$ROOT" && uv run --quiet python -m cashflow.entities add --id "$ENTITY_ID" \
    --name "$ENTITY_NAME" --opening-balance "$OPENING_BALANCE" --as-of "$OPENING_ON")

  say "Publishing the categorizer's approved transactions for $CLIENT_SLUG"
  (cd "$CATEGORIZER_DIR" && php artisan transactions:publish-categorized "$CLIENT_SLUG")
  local expected
  expected="$(cd "$CATEGORIZER_DIR" && php artisan tinker --execute \
    "echo App\\Models\\Client::where('slug', '$CLIENT_SLUG')->sole()->transactions()->where('categorization_status', 'approved')->count();" | tail -n 1)"

  say "Waiting for $expected transactions to arrive through the relay"
  local count pending
  for _ in $(seq 1 90); do
    read -r count pending < <(entity_state)
    printf '\r    %s/%s received, refresh pending: %s   ' "$count" "$expected" "$pending"
    [[ "$count" -ge "$expected" && "$pending" == "no" ]] && break
    sleep 2
  done
  echo
  [[ "$count" -ge "$expected" ]] || die "only $count of $expected arrived; check the relay's deliveries page"

  say "Refreshing as of $DATA_END, where the categorizer's demo data ends"
  (cd "$ROOT" && uv run --quiet python -m cashflow.refresh --entity "$ENTITY_ID" --as-of "$DATA_END")

  say "Weekly summary for the week of $SUMMARY_WEEK"
  local summary
  summary="$(curl -sf "$CASHFLOW_URL/entities/$ENTITY_ID/summary?week_of=$SUMMARY_WEEK")"
  (cd "$ROOT" && uv run --quiet python - "$summary" <<'PY'
import json
import sys

s = json.loads(sys.argv[1])
cash = s["cash"]
week = cash["this_week"]
print(f"    {s['entity_name']}, {s['week_start']} to {s['week_end']}")
print(f"    in {week['money_in']}, out {week['money_out']}; balance {cash['start_balance']} -> {cash['end_balance']}")
if outlook := s["outlook"]:
    h, low = outlook["horizons"][-1], outlook["lowest_expected"]
    print(f"    {h['horizon_days']}-day outlook: {h['expected']} (80% band {h['lower']} to {h['upper']})")
    print(f"    lowest expected balance: {low['balance']} on {low['on_date']}")
print(f"    open anomalies: {s['anomalies']['open_by_severity']}")
for a in s["anomalies"]["top"]:
    print(f"      [{a['severity']}] {a['explanation']}")
PY
  )

  say "Done. Explore: $CASHFLOW_URL/docs, or ask Claude through the MCP server."
}

case "${1:-}" in
  setup) setup ;;
  run) run ;;
  *) echo "usage: scripts/e2e-demo.sh setup|run"; exit 2 ;;
esac
