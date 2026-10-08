from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cashflow.core.events import RelayEnvelope, TransactionCategorized


def data(**overrides: Any) -> dict[str, Any]:
    transaction = {
        "id": "48213",
        "account_id": "3",
        "posted_on": "2025-12-31",
        "amount": "-129.99",
        "currency": "USD",
        "description": "ADOBE *CREATIVE CLD",
        "vendor": "Adobe",
        "category": "Software & Subscriptions",
        "categorized_at": "2025-12-31T18:30:00Z",
    } | overrides
    return {"entity_id": "17", "transaction": transaction}


def test_maps_to_domain_transaction() -> None:
    txn = TransactionCategorized.model_validate(data()).to_transaction()

    assert txn.entity_id == "17"
    assert txn.source_id == "48213"
    assert txn.posted_on == date(2025, 12, 31)
    assert txn.amount == Decimal("-129.99")


@pytest.mark.parametrize("amount", [-129.99, -130, "-129.999", "abc", None])
def test_amount_must_be_a_two_place_decimal_string(amount: object) -> None:
    with pytest.raises(ValidationError, match="amount"):
        TransactionCategorized.model_validate(data(amount=amount))


def test_currency_must_be_iso_code() -> None:
    with pytest.raises(ValidationError, match="currency"):
        TransactionCategorized.model_validate(data(currency="usd"))


def test_envelope_requires_timezone_aware_created_at() -> None:
    body = '{"id": "e1", "type": "t", "created_at": "2026-01-01T00:00:00", "data": {}}'

    with pytest.raises(ValidationError, match="created_at"):
        RelayEnvelope.model_validate_json(body)


@pytest.mark.parametrize("categorized_at", [None, "2025-12-31T18:30:00", "yesterday"])
def test_categorized_at_is_a_required_aware_timestamp(categorized_at: object) -> None:
    with pytest.raises(ValidationError, match="categorized_at"):
        TransactionCategorized.model_validate(data(categorized_at=categorized_at))
