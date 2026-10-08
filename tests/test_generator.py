"""The demo generator is deterministic and its ground truth is exact.

Detection tests in M3 and M5 rely on this: if the generator says it planted one duplicate
charge, there must be exactly one, and no accidental ones.
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cashflow.core.enums import AnomalyType
from cashflow.core.models import Transaction
from cashflow.demo.generator import DemoDataset, generate

if TYPE_CHECKING:
    from decimal import Decimal

AS_OF = date(2026, 10, 8)
DUPLICATE_WINDOW_DAYS = 3

seeds = st.integers(min_value=0, max_value=2**32)
as_of_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31))
month_counts = st.integers(min_value=6, max_value=36)


def planted(ds: DemoDataset, kind: AnomalyType) -> list[Transaction]:
    (anomaly,) = (a for a in ds.anomalies if a.type is kind)
    by_id = {t.source_id: t for t in ds.transactions}
    return [by_id[i] for i in anomaly.source_ids]


def test_same_inputs_give_identical_datasets() -> None:
    assert generate(7, AS_OF) == generate(7, AS_OF)


def test_different_seeds_differ() -> None:
    a, b = generate(7, AS_OF), generate(8, AS_OF)

    assert a.entity.name != b.entity.name
    assert a.transactions != b.transactions


def test_rejects_too_little_history() -> None:
    with pytest.raises(ValueError, match="months"):
        generate(1, AS_OF, months=5)


@settings(max_examples=40, deadline=None)
@given(seeds, as_of_dates, month_counts)
def test_transactions_are_well_formed(seed: int, as_of: date, months: int) -> None:
    ds = generate(seed, as_of, months)
    source_ids = [t.source_id for t in ds.transactions]
    assert len(set(source_ids)) == len(source_ids)

    first, last = ds.transactions[0].posted_on, ds.transactions[-1].posted_on
    assert ds.entity.opening_balance_on < first
    assert all(t.entity_id == ds.entity.id for t in ds.transactions)
    assert all(first <= t.posted_on <= last for t in ds.transactions)
    assert all(t.amount != 0 and t.amount.as_tuple().exponent == -2 for t in ds.transactions)


@settings(max_examples=40, deadline=None)
@given(seeds, as_of_dates, month_counts)
def test_every_anomaly_type_is_planted_once_with_real_evidence(
    seed: int, as_of: date, months: int
) -> None:
    ds = generate(seed, as_of, months)
    source_ids = {t.source_id for t in ds.transactions}

    assert sorted(a.type for a in ds.anomalies) == sorted(AnomalyType)
    for anomaly in ds.anomalies:
        assert anomaly.source_ids
        assert set(anomaly.source_ids) <= source_ids


@settings(max_examples=40, deadline=None)
@given(seeds, as_of_dates, month_counts)
def test_only_planted_duplicates_exist(seed: int, as_of: date, months: int) -> None:
    ds = generate(seed, as_of, months)
    seen: dict[tuple[str, Decimal], list[Transaction]] = defaultdict(list)
    found: set[str] = set()
    for t in ds.transactions:
        if t.amount >= 0:
            continue
        for prior in seen[(t.vendor, t.amount)]:
            if (t.posted_on - prior.posted_on).days <= DUPLICATE_WINDOW_DAYS:
                found |= {prior.source_id, t.source_id}
        seen[(t.vendor, t.amount)].append(t)

    assert found == {t.source_id for t in planted(ds, AnomalyType.DUPLICATE_CHARGE)}


@settings(max_examples=40, deadline=None)
@given(seeds, as_of_dates, month_counts)
def test_recurring_ground_truth_matches_transactions(seed: int, as_of: date, months: int) -> None:
    ds = generate(seed, as_of, months)
    for series in ds.recurring:
        charges = [t for t in ds.transactions if t.vendor == series.vendor]
        assert len(charges) == series.occurrences
        assert series.occurrences >= 1


@settings(max_examples=40, deadline=None)
@given(seeds, as_of_dates, month_counts)
def test_planted_anomalies_have_their_defining_shape(seed: int, as_of: date, months: int) -> None:
    ds = generate(seed, as_of, months)
    typical = {s.vendor: s.typical_amount for s in ds.recurring}
    last_day = ds.transactions[-1].posted_on

    for t in planted(ds, AnomalyType.RECURRING_AMOUNT_CHANGE):
        assert t.amount != typical[t.vendor]

    (last_seen,) = planted(ds, AnomalyType.MISSED_RECURRING)
    later = [
        t
        for t in ds.transactions
        if t.vendor == last_seen.vendor and t.posted_on > last_seen.posted_on
    ]
    assert later == []
    assert last_day - last_seen.posted_on > timedelta(days=35)

    (new_vendor,) = planted(ds, AnomalyType.NEW_VENDOR_LARGE)
    assert [t for t in ds.transactions if t.vendor == new_vendor.vendor] == [new_vendor]

    spike = planted(ds, AnomalyType.CATEGORY_SPIKE)
    assert len({(t.posted_on.year, t.posted_on.month) for t in spike}) == 1
