"""Deterministic synthetic transactions for one small business, with planted anomalies.

`generate(seed, as_of)` is pure: the same arguments always yield the same dataset. Alongside
the transactions it returns ground truth (the recurring series and the anomalies it planted)
so detection tests can assert exact recovery.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from random import Random
from statistics import median

from cashflow.core.calendar import add_months
from cashflow.core.enums import AnomalyType, Cadence
from cashflow.core.models import Entity, Transaction

CENT = Decimal("0.01")
MIN_MONTHS = 6
DEFAULT_MONTHS = 24
CURRENCY = "USD"
SATURDAY = 5
# Random charges never repeat a vendor + amount this close together; only planted ones do.
DUPLICATE_GUARD_DAYS = 14
# Random discretionary spend never exceeds this multiple of its category's trailing typical
# month, so the only category spike is the planted one.
MONTHLY_SPEND_CAP = Decimal("1.75")

BUSINESS_NAMES = (
    "Brightline Design Studio",
    "Copperleaf Consulting",
    "Harbor & Pine Architecture",
    "Northstar Creative",
    "Fieldwork Analytics",
)
CLIENTS = (
    "Northwind Traders",
    "Acme Corp",
    "Globex",
    "Umbrella Health",
    "Stark Logistics",
    "Wayne Foods",
)

# Discretionary spend: (category, vendors, weekday probability, weekend probability, amount range)
VARIABLE_SPEND = (
    ("Meals", ("Blue Bottle Coffee", "Sweetgreen", "Chipotle", "Corner Deli"), 0.55, 0.15, (8, 60)),
    ("Office Supplies", ("Staples", "Office Depot", "Amazon"), 0.10, 0.02, (15, 180)),
    ("Fuel", ("Shell", "Chevron"), 0.18, 0.10, (35, 90)),
    ("Travel", ("Delta", "United", "Marriott"), 0.015, 0.01, (180, 900)),
)


@dataclass(frozen=True)
class ExpectedSeries:
    """A recurring series the generator planted, with its pre-anomaly amount."""

    vendor: str
    cadence: Cadence
    typical_amount: Decimal
    occurrences: int


@dataclass(frozen=True)
class PlantedAnomaly:
    type: AnomalyType
    subject: str
    """The vendor or category the anomaly is about."""
    source_ids: tuple[str, ...]
    """Evidence transactions. For a missed bill, the last occurrence before the gap."""


@dataclass(frozen=True)
class DemoDataset:
    entity: Entity
    transactions: tuple[Transaction, ...]
    recurring: tuple[ExpectedSeries, ...]
    anomalies: tuple[PlantedAnomaly, ...]


@dataclass(frozen=True)
class _RecurringSpec:
    vendor: str
    category: str
    cadence: Cadence
    amount: Decimal
    day: int = 1
    """Day of month for monthly-or-longer cadences."""
    month_offset: int = 0
    """Which month in the cycle the series starts on (quarterly and annual)."""


@dataclass
class _Draft:
    posted_on: date
    amount: Decimal
    vendor: str
    category: str
    description: str
    account: str
    tags: set[AnomalyType] = field(default_factory=set)
    protected: bool = False
    """Never dropped by the monthly cap (the guaranteed restock)."""


def generate(seed: int, as_of: date, months: int = DEFAULT_MONTHS) -> DemoDataset:
    if months < MIN_MONTHS:
        msg = f"months must be at least {MIN_MONTHS} to leave room for planted anomalies"
        raise ValueError(msg)
    return _Generator(seed, as_of, months).build()


def _money(value: float) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _month_start(d: date) -> date:
    return d.replace(day=1)


class _Generator:
    def __init__(self, seed: int, as_of: date, months: int) -> None:
        self.seed = seed
        self.rng = Random(seed)
        self.as_of = as_of
        self.start = add_months(as_of, -months) + timedelta(days=1)
        self.entity_id = f"demo-{seed}"
        self.checking = f"{self.entity_id}-checking"
        self.card = f"{self.entity_id}-card"
        self.drafts: list[_Draft] = []

    def build(self) -> DemoDataset:
        rng = self.rng
        entity = Entity(
            id=self.entity_id,
            name=BUSINESS_NAMES[self.seed % len(BUSINESS_NAMES)],  # distinct for consecutive seeds
            currency=CURRENCY,
            opening_balance=_money(rng.uniform(30_000, 60_000)),
            opening_balance_on=self.start - timedelta(days=1),
        )

        specs = self._recurring_specs()
        for spec in specs:
            self._add_recurring(spec)
        self._add_client_revenue()
        self._add_cloud_hosting()
        self._add_variable_spend()
        self._cap_discretionary_months()

        self._plant_price_increase("Adobe")
        self._plant_missed_bill("Google Workspace")
        self._plant_duplicate_charge()
        self._plant_category_spike("Office Supplies")
        self._plant_new_vendor_large()
        self._break_accidental_duplicates()

        transactions, ids_by_draft = self._finalize()
        return DemoDataset(
            entity=entity,
            transactions=transactions,
            recurring=self._expected_series(specs),
            anomalies=self._planted(ids_by_draft),
        )

    # Baseline flows -------------------------------------------------------------------------

    def _recurring_specs(self) -> tuple[_RecurringSpec, ...]:
        rng = self.rng
        payroll = _money(rng.uniform(8_000, 10_500))
        rent = _money(rng.choice((3_200, 3_500, 3_850, 4_100)))
        retainer = _money(rng.choice((3_500, 4_000, 4_500)))
        return (
            _RecurringSpec("Initech", "Revenue", Cadence.MONTHLY, retainer, day=1),
            _RecurringSpec("Gusto Payroll", "Payroll", Cadence.BIWEEKLY, -payroll),
            _RecurringSpec("Parkside Properties", "Rent", Cadence.MONTHLY, -rent, day=1),
            _RecurringSpec("Comcast Business", "Utilities", Cadence.MONTHLY, Decimal("-129.95"), 8),
            _RecurringSpec(
                "Adobe", "Software & Subscriptions", Cadence.MONTHLY, Decimal("-89.99"), 12
            ),
            _RecurringSpec(
                "Google Workspace",
                "Software & Subscriptions",
                Cadence.MONTHLY,
                Decimal("-72.00"),
                3,
            ),
            _RecurringSpec(
                "Slack", "Software & Subscriptions", Cadence.MONTHLY, Decimal("-43.75"), 20
            ),
            _RecurringSpec(
                "Figma", "Software & Subscriptions", Cadence.MONTHLY, Decimal("-45.00"), 15
            ),
            _RecurringSpec(
                "Hiscox", "Insurance", Cadence.QUARTERLY, Decimal("-612.00"), 10, rng.randrange(3)
            ),
            _RecurringSpec(
                "Squarespace",
                "Software & Subscriptions",
                Cadence.ANNUAL,
                Decimal("-276.00"),
                22,
                rng.randrange(12),
            ),
        )

    def _occurrences(self, spec: _RecurringSpec) -> list[date]:
        if spec.cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY):
            step = timedelta(weeks=1 if spec.cadence is Cadence.WEEKLY else 2)
            first = self.start + timedelta(days=(4 - self.start.weekday()) % 7)  # first Friday
            return [first + step * i for i in range((self.as_of - first) // step + 1)]

        step_months = {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.ANNUAL: 12}[spec.cadence]
        anchor = add_months(self.start.replace(day=spec.day), spec.month_offset)
        while anchor < self.start:
            anchor = add_months(anchor, step_months)
        dates: list[date] = []
        while (d := add_months(anchor, step_months * len(dates))) <= self.as_of:
            dates.append(d)
        return dates

    def _add_recurring(self, spec: _RecurringSpec) -> None:
        kind = "ACH CREDIT" if spec.amount > 0 else "ACH DEBIT"
        account = self.checking if spec.category in {"Revenue", "Payroll", "Rent"} else self.card
        for posted_on in self._occurrences(spec):
            self._add(
                posted_on, spec.amount, spec.vendor, spec.category, kind=kind, account=account
            )

    def _add_client_revenue(self) -> None:
        month = _month_start(self.start)
        while month <= self.as_of:
            days_in_month = calendar.monthrange(month.year, month.month)[1]
            candidates = (month.replace(day=n) for n in range(1, days_in_month + 1))
            weekdays = [
                d for d in candidates if self.start <= d <= self.as_of and d.weekday() < SATURDAY
            ]
            for _ in range(self.rng.randint(4, 7) if weekdays else 0):
                client = self.rng.choice(CLIENTS)
                amount = _money(self.rng.uniform(1_500, 8_000))
                posted_on = self.rng.choice(weekdays)
                self._add(
                    posted_on, amount, client, "Revenue", kind="ACH CREDIT", account=self.checking
                )
            month = add_months(month, 1)

    def _add_cloud_hosting(self) -> None:
        # Monthly but usage-priced: quiet and busy months alternate, so at most about half the
        # charges sit near the median, and it can never pass the recurring amount rule.
        spec = _RecurringSpec("AWS", "Cloud Hosting", Cadence.MONTHLY, Decimal(0), day=2)
        for n, posted_on in enumerate(self._occurrences(spec)):
            low, high = (130, 180) if n % 2 == 0 else (270, 340)
            amount = -_money(self.rng.uniform(low, high))
            self._add(posted_on, amount, "AWS", "Cloud Hosting")

    def _add_variable_spend(self) -> None:
        day = self.start
        while day <= self.as_of:
            weekend = day.weekday() >= SATURDAY
            if day.day == 1:
                # A restock at the start of every month, so supplies always have a baseline.
                restock = -_money(self.rng.uniform(40, 120))
                vendor = self.rng.choice(("Staples", "Office Depot"))
                self._add(day, restock, vendor, "Office Supplies")
                self.drafts[-1].protected = True
            for category, vendors, p_weekday, p_weekend, (low, high) in VARIABLE_SPEND:
                p = p_weekend if weekend else p_weekday
                if self.rng.random() < p:
                    vendor = self.rng.choice(vendors)
                    amount = -_money(self.rng.uniform(low, high))
                    self._add(day, amount, vendor, category)
            day += timedelta(days=1)

    def _cap_discretionary_months(self) -> None:
        """Keep each discretionary month within MONTHLY_SPEND_CAP x its trailing 6-month median.

        Freak months (say 14 fuel stops where 5 is normal) would be real category spikes, so
        they can't appear by chance in data whose ground truth must be exact. Months are capped
        in order, against already-capped history, by dropping their latest charges. (Shrinking
        charges instead would create runs of identical amounts that look recurring.) The window
        matches the one the spike rule compares against."""
        categories = {category for category, *_ in VARIABLE_SPEND}
        by_month: dict[tuple[str, date], list[_Draft]] = defaultdict(list)
        for draft in self.drafts:
            if draft.category in categories:
                by_month[(draft.category, _month_start(draft.posted_on))].append(draft)

        def spend(category: str, month: date) -> Decimal:
            return -sum((d.amount for d in by_month.get((category, month), [])), Decimal(0))

        month = _month_start(self.start)
        while month <= self.as_of:
            trailing = [add_months(month, -k) for k in range(1, 7)]
            for category in sorted(categories):
                typical = Decimal(median(spend(category, m) for m in trailing))
                drafts = sorted(
                    (d for d in by_month.get((category, month), []) if not d.protected),
                    key=lambda d: d.posted_on,
                )
                while (
                    typical > 0 and drafts and spend(category, month) > typical * MONTHLY_SPEND_CAP
                ):
                    dropped = drafts.pop()
                    by_month[(category, month)].remove(dropped)
                    self.drafts.remove(dropped)
            month = add_months(month, 1)

    def _break_accidental_duplicates(self) -> None:
        """Nudge random charges that repeat a vendor + amount within a fortnight of another.

        Runs after planting, so planted anomalies and fixed bills are never touched and the
        only duplicate charges left are the planted ones.
        """
        taken: dict[tuple[str, Decimal], list[date]] = defaultdict(list)
        adjustable: list[_Draft] = []
        for draft in self.drafts:
            if draft.tags or draft.description != "POS":
                taken[(draft.vendor, draft.amount)].append(draft.posted_on)
            else:
                adjustable.append(draft)
        for draft in sorted(adjustable, key=lambda d: d.posted_on):
            while any(
                abs((draft.posted_on - other).days) <= DUPLICATE_GUARD_DAYS
                for other in taken[(draft.vendor, draft.amount)]
            ):
                draft.amount -= CENT
            taken[(draft.vendor, draft.amount)].append(draft.posted_on)

    # Planted anomalies ----------------------------------------------------------------------

    def _plant_price_increase(self, vendor: str) -> None:
        cutoff = self.as_of - timedelta(days=60)
        for draft in self._by_vendor(vendor):
            if draft.posted_on > cutoff:
                draft.amount = Decimal("-104.99")
                draft.tags.add(AnomalyType.RECURRING_AMOUNT_CHANGE)

    def _plant_missed_bill(self, vendor: str) -> None:
        """Stop the series: drop every charge from the last one that is well past due."""
        overdue = self.as_of - timedelta(days=10)
        charges = self._by_vendor(vendor)
        stop_at = max(d.posted_on for d in charges if d.posted_on <= overdue)
        self.drafts = [
            d for d in self.drafts if not (d.vendor == vendor and d.posted_on >= stop_at)
        ]
        last = max(self._by_vendor(vendor), key=lambda d: d.posted_on)
        last.tags.add(AnomalyType.MISSED_RECURRING)

    def _plant_duplicate_charge(self) -> None:
        posted_on = self.as_of - timedelta(days=self.rng.randint(8, 25))
        amount = -_money(self.rng.uniform(55, 85))
        for offset in (0, 1):
            self._add(
                posted_on + timedelta(days=offset),
                amount,
                "Shell",
                "Fuel",
                tag=AnomalyType.DUPLICATE_CHARGE,
            )

    def _plant_category_spike(self, category: str) -> None:
        """A burst of large purchases in the last complete calendar month.

        Vendors rotate so none gets more than two: four similar charges from one vendor a few
        days apart would pass as a weekly series and hide the spike as "recurring"."""
        month = add_months(_month_start(self.as_of), -1)
        vendors = ("Amazon", "Staples", "Office Depot")
        for n in range(5):
            posted_on = month + timedelta(days=self.rng.randint(8, 27))
            amount = -_money(self.rng.uniform(400, 750))
            self._add(posted_on, amount, vendors[n % 3], category, tag=AnomalyType.CATEGORY_SPIKE)

    def _plant_new_vendor_large(self) -> None:
        posted_on = self.as_of - timedelta(days=self.rng.randint(3, 6))
        self._add(
            posted_on,
            Decimal("-4850.00"),
            "Herman Miller",
            "Furniture & Equipment",
            tag=AnomalyType.NEW_VENDOR_LARGE,
        )

    # Assembly -------------------------------------------------------------------------------

    def _add(
        self,
        posted_on: date,
        amount: Decimal,
        vendor: str,
        category: str,
        *,
        kind: str = "POS",
        account: str | None = None,
        tag: AnomalyType | None = None,
    ) -> None:
        tags = {tag} if tag else set()
        draft = _Draft(posted_on, amount, vendor, category, kind, account or self.card, tags)
        self.drafts.append(draft)

    def _by_vendor(self, vendor: str) -> list[_Draft]:
        return [d for d in self.drafts if d.vendor == vendor]

    def _finalize(self) -> tuple[tuple[Transaction, ...], dict[int, str]]:
        ordered = sorted(
            self.drafts, key=lambda d: (d.posted_on, d.account, d.vendor, d.amount, d.category)
        )
        transactions: list[Transaction] = []
        ids_by_draft: dict[int, str] = {}
        for n, draft in enumerate(ordered, start=1):
            source_id = f"{self.entity_id}-txn-{n:05d}"
            ids_by_draft[id(draft)] = source_id
            transactions.append(
                Transaction(
                    entity_id=self.entity_id,
                    source_id=source_id,
                    account_id=draft.account,
                    posted_on=draft.posted_on,
                    amount=draft.amount,
                    description=f"{draft.description} {draft.vendor.upper()}",
                    vendor=draft.vendor,
                    category=draft.category,
                )
            )
        return tuple(transactions), ids_by_draft

    def _expected_series(self, specs: tuple[_RecurringSpec, ...]) -> tuple[ExpectedSeries, ...]:
        # A short history can miss a long cadence entirely (an annual bill); omit those.
        return tuple(
            ExpectedSeries(s.vendor, s.cadence, s.amount, n)
            for s in specs
            if (n := len(self._by_vendor(s.vendor)))
        )

    def _planted(self, ids_by_draft: dict[int, str]) -> tuple[PlantedAnomaly, ...]:
        evidence: dict[AnomalyType, list[_Draft]] = defaultdict(list)
        for draft in self.drafts:
            for tag in draft.tags:
                evidence[tag].append(draft)
        return tuple(
            PlantedAnomaly(
                type=kind,
                subject=drafts[0].category
                if kind is AnomalyType.CATEGORY_SPIKE
                else drafts[0].vendor,
                source_ids=tuple(sorted(ids_by_draft[id(d)] for d in drafts)),
            )
            for kind in AnomalyType
            if (drafts := evidence.get(kind))
        )
