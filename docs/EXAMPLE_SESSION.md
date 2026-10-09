# Example session: Claude using the MCP tools

A real Claude Code session against this project's MCP server and the demo data (`python -m cashflow.demo.seed`, plus Northwind Coffee Co. from the [end-to-end demo](DEMO.md)). It was recorded headless with `claude -p ... --allowedTools "mcp__cashflow-insights__*"` on 9 October 2026, three turns in one conversation.

The tool calls and Claude's answers are verbatim. The tool results (JSON) are left out for length; each call lists its arguments.


## 1. Which businesses can you see, and how did each one do last week?

> **You:** Which businesses can you see, and how did each one do last week? Keep it brief.

**Tools called:** `list_entities()` → `get_weekly_summary(entity_id="1")` → `get_weekly_summary(entity_id="demo-42")` → `get_weekly_summary(entity_id="demo-43")`

**Claude:**

I can see three businesses. Figures are for last week, Mon Sep 28 – Sun Oct 4.

**Northwind Coffee Co.** (`1`): no activity. Its newest transaction is from Mar 31, 2026, so the data is about six months old. The balance stayed at $130,837 and there are no open anomalies.

**Harbor & Pine Architecture** (`demo-42`): a strong week.
- Net was **+$18,371**: $23,226 in and $4,855 out. A typical week nets about +$3,500.
- Balance went from $163,928 to $182,299. The forecast is about $198.5k by early January.
- 5 open anomalies, 2 of them high severity:
  - A first-ever $4,850 Herman Miller charge on Oct 5 (the day after the week ended).
  - September office supplies came to $3,331, against a typical $242.

**Northstar Creative** (`demo-43`): spent more than it took in, for the second week running.
- Net was **−$3,349**: $5,687 in and $9,035 out. The week before was −$8,327.
- Balance went from $85,065 to $81,716. Most of the drop is a one-off $4,850 Herman Miller purchase on Oct 4.
- The forecast still recovers to about $104.7k by early January. The bands are wide, and the lower band dips to about $75.9k.
- It has the same 5 anomaly types as Harbor & Pine, again 2 of them high severity (Herman Miller, and $2,678 of September office supplies).

**Low-severity anomalies at both demo firms:**
- Adobe went up 16.7%.
- Google Workspace has missed two monthly charges.
- Shell charged the same amount twice.

**Possible bug:** the "upcoming" list for both demo firms still shows Adobe at $89.99, even though the anomaly says the charge rose to $104.99 in August. The recurring series might not be picking up the new amount.


## 2. Will Harbor & Pine run low on cash in the next 90 days? How much should I trust that forecast?

> **You:** Will Harbor & Pine run low on cash in the next 90 days? How much should I trust that forecast? Keep it brief.

**Tools called:** `get_forecast(entity_id="demo-42", horizon_days=90)`

**Claude:**

**No, Harbor & Pine doesn't look likely to run low.** The balance is $186.9k today (Oct 8). The expected path dips no lower than about **$179.3k** (Oct 11) and finishes around **$198.5k** on Jan 6. Even the cautious edge of the forecast range never drops below **$168.8k** (Dec 6). The regular dips of about $7k every two weeks, which look like payroll, are small next to that cushion.

**How much to trust it: fairly well for the overall direction, less for exact amounts.** The service checked the model against Harbor & Pine's past data:
- **Typical miss: about $6k at 90 days**, roughly 3% of the balance.
- **It beats a simple baseline:** its errors are about 40% smaller than just carrying recent movement forward.
- **The range is cautious:** it's meant to contain the actual balance 80% of the time, but in past data it did so 96% of the time. The real low point is probably above $168.8k.

**Caveats:**
- **One-off events aren't predicted.** The model only extends past patterns, so it can't foresee things like a client leaving, a late invoice, or another large purchase like the $4,850 Herman Miller charge.
- **One strong week could inflate the trend.** Last week's $23k of income was more than double the usual amount. If that was a one-time payment, the upward trend may be too optimistic.
- **This is demo data.** Harbor & Pine is a synthetic business from the demo seeder, so these accuracy numbers show the model works on generated data, not real books.


## 3. Explain Northstar Creative's high-severity anomalies, with the transactions behind each.

> **You:** Explain Northstar Creative's high-severity anomalies, with the transactions behind each. Keep it brief.

**Tools called:** `explain_anomaly(entity_id="demo-43", anomaly_id=29)` → `explain_anomaly(entity_id="demo-43", anomaly_id=26)`

**Claude:**

Northstar Creative has two open high-severity anomalies. Both come from the synthetic demo data, and Harbor & Pine has the same two at slightly different amounts.

**1. Large charge from a new vendor (#29)**
Northstar's first-ever payment to Herman Miller was big enough to flag.

| Txn | Date | Vendor | Category | Amount |
|---|---|---|---|---|
| 1745 | Oct 4 | Herman Miller | Furniture & Equipment | −$4,850.00 |

This one charge accounts for most of last week's −$3,349 net. If it was a planned office chair or desk purchase, it can be dismissed.

**2. Office Supplies spike (#26)**
September office-supply spending was **$2,678.47**, against a typical **$128** a month (the median of the previous six months). That's about 21× normal.

| Txn | Date | Vendor | Amount |
|---|---|---|---|
| 1691 | Sep 1 | Staples | −$83.48 |
| 1707 | Sep 10 | Office Depot | −$519.85 |
| 1715 | Sep 16 | Amazon | −$444.62 |
| 1719 | Sep 17 | Amazon | −$664.38 |
| 1721 | Sep 18 | Staples | −$431.57 |
| 1731 | Sep 25 | Staples | −$534.57 |

Only the Sep 1 charge looks normal. The other five, from Sep 10 to 25, are all $430–$665. Several large purchases from different stores in two weeks could mean an office setup that matches the Herman Miller purchase, or furniture and equipment filed under the wrong category. It could also mean spending that's worth asking about.

Neither anomaly has been dismissed yet. The MCP tools are read-only, so dismissing them would have to happen through the API.


---

**About the "possible bug" in answer 1.** Claude was right. The upcoming-bills list projected Adobe at its old $89.99 median even though two charges had confirmed the new $104.99 price. That is fixed since this session was recorded: a series still keeps its median as `typical_amount`, so the anomaly can flag the change, but once two consecutive charges confirm a new price it becomes the series' `projected_amount`, which the forecast, the upcoming list, and missed-bill explanations use (see "Recurring detection" in [ARCHITECTURE.md](ARCHITECTURE.md)). The session above is kept as recorded.
