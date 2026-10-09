"""Anomaly rules. Each is a pure function of an entity's transactions and recurring series.

Shared rules (see "Anomaly rules" in docs/ARCHITECTURE.md):

- Only findings with recent evidence (`lookback_days`) are reported, so a first scan of a long
  history surfaces what matters now, not every old oddity.
- Severity comes from the finding's dollar impact, the same way for every rule.
- Each finding has a fingerprint: a stable key for "the same problem", so rescans update an
  open anomaly instead of duplicating it and never reopen a dismissed one.
"""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

import polars as pl

from cashflow.core.calendar import add_months
from cashflow.core.enums import AnomalyType, Cadence, Severity
from cashflow.core.models import Transaction
from cashflow.core.recurring import DetectedSeries
from cashflow.settings import AnomalySettings

MAD_TO_SIGMA = 1.4826  # scales a median absolute deviation to a normal standard deviation
CHARGES_PER_YEAR = {
    Cadence.WEEKLY: 52,
    Cadence.BIWEEKLY: 26,
    Cadence.MONTHLY: 12,
    Cadence.QUARTERLY: 4,
    Cadence.ANNUAL: 1,
}


@dataclass(frozen=True)
class Finding:
    type: AnomalyType
    severity: Severity
    subject: str
    """The vendor or category the finding is about."""
    explanation: str
    source_ids: tuple[str, ...]
    """Evidence transactions. For a missed bill, the last charge that did arrive."""
    fingerprint: str


def scan(
    transactions: Sequence[Transaction],
    series: Sequence[DetectedSeries],
    *,
    as_of: date,
    settings: AnomalySettings,
) -> list[Finding]:
    past = [t for t in transactions if t.posted_on <= as_of]
    if not past:
        return []
    context = _Context(past, as_of, settings)
    findings = [
        *duplicate_charges(context),
        *category_spikes(context, series),
        *new_vendors_large(context),
        *missed_recurring(context, series),
        *recurring_amount_changes(context, series),
    ]
    return sorted(findings, key=lambda f: (f.type, f.subject, f.fingerprint))


@dataclass(frozen=True)
class _Context:
    transactions: Sequence[Transaction]
    as_of: date
    settings: AnomalySettings

    @property
    def window_start(self) -> date:
        """Evidence on or after this date is recent enough to report."""
        return self.as_of - timedelta(days=self.settings.lookback_days - 1)

    @property
    def history_start(self) -> date:
        return min(t.posted_on for t in self.transactions)

    def severity(self, impact: Decimal) -> Severity:
        if impact >= self.settings.severity_high_at:
            return Severity.HIGH
        if impact >= self.settings.severity_medium_at:
            return Severity.MEDIUM
        return Severity.LOW

    def finding(
        self,
        kind: AnomalyType,
        subject: str,
        *,
        impact: Decimal,
        explanation: str,
        evidence: Sequence[Transaction],
        key: str,
    ) -> Finding:
        return Finding(
            type=kind,
            severity=self.severity(abs(impact)),
            subject=subject,
            explanation=explanation,
            source_ids=tuple(sorted(t.source_id for t in evidence)),
            fingerprint=fingerprint(kind, key),
        )


def fingerprint(kind: AnomalyType, key: str) -> str:
    return hashlib.sha256(f"{kind.value}|{key}".encode()).hexdigest()


# duplicate_charge ------------------------------------------------------------------------------


def duplicate_charges(ctx: _Context) -> list[Finding]:
    """Same vendor and amount within `duplicate_window_days`. Each cluster is one finding,
    keyed by its first charge so a third copy extends it rather than starting a new one."""
    s = ctx.settings
    by_vendor_amount: dict[tuple[str, Decimal], list[Transaction]] = defaultdict(list)
    for t in ctx.transactions:
        if t.amount < 0 and -t.amount >= s.duplicate_min_amount:
            by_vendor_amount[(t.vendor, t.amount)].append(t)

    findings = []
    for (vendor, amount), charges in by_vendor_amount.items():
        for cluster in _clusters(charges, s.duplicate_window_days):
            if len(cluster) < 2 or cluster[-1].posted_on < ctx.window_start:  # noqa: PLR2004
                continue
            days = ", ".join(_day(t.posted_on) for t in cluster)
            findings.append(
                ctx.finding(
                    AnomalyType.DUPLICATE_CHARGE,
                    vendor,
                    impact=-amount * (len(cluster) - 1),
                    explanation=f"{vendor} charged {_money(amount)} {len(cluster)} times ({days}).",
                    evidence=cluster,
                    key=f"{vendor}|{cluster[0].source_id}",
                )
            )
    return findings


def _clusters(charges: list[Transaction], window_days: int) -> list[list[Transaction]]:
    """Group charges whose consecutive dates are at most `window_days` apart."""
    clusters: list[list[Transaction]] = []
    for charge in sorted(charges, key=lambda t: (t.posted_on, t.source_id)):
        if clusters and (charge.posted_on - clusters[-1][-1].posted_on).days <= window_days:
            clusters[-1].append(charge)
        else:
            clusters.append([charge])
    return clusters


# category_spike --------------------------------------------------------------------------------


def category_spikes(ctx: _Context, series: Sequence[DetectedSeries]) -> list[Finding]:
    """A month's discretionary category spend far above its trailing baseline (robust z-score).

    Recurring charges are excluded: an annual bill landing is expected, not a spike, and price
    changes have their own rule. Checks the last complete month and the month in progress
    (spend only accumulates, so a partial month already above the threshold is a spike)."""
    s = ctx.settings
    recurring = {(r.vendor, r.typical_amount > 0) for r in series}
    outflows = [t for t in ctx.transactions if t.amount < 0 and (t.vendor, False) not in recurring]
    if not outflows:
        return []
    monthly = (
        pl.DataFrame(
            [(t.category, t.posted_on.replace(day=1), float(-t.amount)) for t in outflows],
            schema={"category": pl.String, "month": pl.Date, "spend": pl.Float64},
            orient="row",
        )
        .group_by("category", "month")
        .agg(pl.col("spend").sum())
    )
    spend = {(c, m): Decimal(str(round(v, 2))) for c, m, v in monthly.iter_rows()}
    categories = sorted({c for c, _ in spend})

    current = ctx.as_of.replace(day=1)
    findings = []
    for month in (add_months(current, -1), current):
        baseline_months = [add_months(month, -k) for k in range(1, s.category_baseline_periods + 1)]
        if baseline_months[-1] < ctx.history_start.replace(day=1):
            continue  # not enough history for a baseline yet
        for category in categories:
            actual = spend.get((category, month), Decimal(0))
            baseline = [spend.get((category, m), Decimal(0)) for m in baseline_months]
            if sum(1 for b in baseline if b > 0) < s.category_min_active_periods:
                continue
            typical = Decimal(median(baseline))
            mad = Decimal(median(abs(b - typical) for b in baseline))
            spread = max(mad, typical * s.category_mad_floor) * Decimal(MAD_TO_SIGMA)
            excess = actual - typical
            if (
                excess < s.category_min_excess
                or spread == 0
                or excess / spread < Decimal(s.category_spike_z)
            ):
                continue
            evidence = [
                t
                for t in outflows
                if t.category == category and t.posted_on.replace(day=1) == month
            ]
            period = f"so far in {month:%B %Y}" if month == current else f"in {month:%B %Y}"
            findings.append(
                ctx.finding(
                    AnomalyType.CATEGORY_SPIKE,
                    category,
                    impact=excess,
                    explanation=f"{category} spend {period} was {_money(actual)}, against a "
                    f"typical {_money(typical)} (median of the previous {len(baseline)} months).",
                    evidence=evidence,
                    key=f"{category}|{month:%Y-%m}",
                )
            )
    return findings


# new_vendor_large ------------------------------------------------------------------------------


def new_vendors_large(ctx: _Context) -> list[Finding]:
    """A vendor's first-ever charge is large. Not during warm-up, when every vendor is new."""
    s = ctx.settings
    warm_from = ctx.history_start + timedelta(days=s.warmup_days)
    first: dict[str, Transaction] = {}
    for t in sorted(ctx.transactions, key=lambda t: (t.posted_on, t.amount, t.source_id)):
        if t.amount < 0:
            first.setdefault(t.vendor, t)

    return [
        ctx.finding(
            AnomalyType.NEW_VENDOR_LARGE,
            vendor,
            impact=-t.amount,
            explanation=f"First-ever charge from {vendor}: {_money(t.amount)} on "
            f"{_day(t.posted_on)} ({t.category}).",
            evidence=[t],
            key=vendor,
        )
        for vendor, t in first.items()
        if t.posted_on >= max(warm_from, ctx.window_start)
        and -t.amount >= s.new_vendor_large_amount
    ]


# missed_recurring ------------------------------------------------------------------------------


def missed_recurring(ctx: _Context, series: Sequence[DetectedSeries]) -> list[Finding]:
    """A recurring bill or income is past its expected date plus a grace period."""
    grace = timedelta(days=ctx.settings.missed_recurring_grace_days)
    findings = []
    for s in series:
        due = s.next_expected_on
        if ctx.as_of <= due + grace:
            continue
        missed = len(s.occurrences_between(due, ctx.as_of - grace))
        last = _last_charge(ctx, s)
        noun = "payment" if s.typical_amount > 0 else "charge"
        findings.append(
            ctx.finding(
                AnomalyType.MISSED_RECURRING,
                s.vendor,
                impact=abs(s.projected_amount) * missed,
                explanation=f"{s.vendor}'s {s.cadence.value} {noun} of "
                f"{_money(s.projected_amount)} was expected on {_day(due)} and hasn't arrived "
                f"({missed} expected {noun}{'s' if missed != 1 else ''} missed; last seen "
                f"{_day(s.last_seen_on)}).",
                evidence=[last] if last else [],
                key=f"{s.vendor}|{_direction(s.typical_amount)}|{due.isoformat()}",
            )
        )
    return findings


# recurring_amount_change -----------------------------------------------------------------------


def recurring_amount_changes(ctx: _Context, series: Sequence[DetectedSeries]) -> list[Finding]:
    """The latest charges of a series differ from its typical amount beyond tolerance.

    Evidence is the unbroken run of changed charges ending at the latest one; the fingerprint
    is keyed on the first of them, so more charges at the new price extend the same anomaly."""
    tolerance = ctx.settings.recurring_amount_tolerance
    findings = []
    for s in series:
        charges = _charges(ctx, s)
        changed: list[Transaction] = []
        for t in reversed(charges):
            if abs(t.amount - s.typical_amount) <= abs(s.typical_amount) * tolerance:
                break
            changed.insert(0, t)
        if not changed or changed[-1].posted_on < ctx.window_start:
            continue
        new_amount = changed[-1].amount
        change = (new_amount - s.typical_amount) / abs(s.typical_amount)
        yearly = abs(new_amount - s.typical_amount) * CHARGES_PER_YEAR[s.cadence]
        verb = "rose" if abs(new_amount) > abs(s.typical_amount) else "fell"
        findings.append(
            ctx.finding(
                AnomalyType.RECURRING_AMOUNT_CHANGE,
                s.vendor,
                impact=yearly,
                explanation=f"{s.vendor}'s {s.cadence.value} amount {verb} from "
                f"{_money(s.typical_amount)} to {_money(new_amount)} ({abs(change):.1%}) "
                f"starting {_day(changed[0].posted_on)}, about {_money(yearly)} a year.",
                evidence=changed,
                key=f"{s.vendor}|{_direction(s.typical_amount)}|{changed[0].source_id}",
            )
        )
    return findings


# Helpers ---------------------------------------------------------------------------------------


def _charges(ctx: _Context, s: DetectedSeries) -> list[Transaction]:
    """The series' transactions: same vendor and direction, as recurring detection groups them."""
    return sorted(
        (
            t
            for t in ctx.transactions
            if t.vendor == s.vendor and t.amount != 0 and (t.amount > 0) == (s.typical_amount > 0)
        ),
        key=lambda t: (t.posted_on, t.source_id),
    )


def _last_charge(ctx: _Context, s: DetectedSeries) -> Transaction | None:
    charges = _charges(ctx, s)
    return charges[-1] if charges else None


def _direction(amount: Decimal) -> str:
    return "in" if amount > 0 else "out"


def _money(amount: Decimal) -> str:
    return f"${abs(amount):,.2f}"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"
