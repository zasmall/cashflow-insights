"""Inbound event models: the relay's envelope and the payloads we understand."""

from datetime import date
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    JsonValue,
    StringConstraints,
)

from cashflow.core.models import CurrencyCode, Label, Money, SourceId, Transaction

TRANSACTION_CATEGORIZED = "transaction.categorized"


def _require_string(value: object) -> object:
    # JSON numbers are binary floats; money must arrive as an exact decimal string.
    if not isinstance(value, str):
        raise ValueError("amount must be a decimal string, not a JSON number")
    return value


class RelayEnvelope(BaseModel):
    """What the relay POSTs: its own event metadata wrapped around the source payload."""

    model_config = ConfigDict(frozen=True)

    id: SourceId
    type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    created_at: AwareDatetime
    data: dict[str, JsonValue]


class CategorizedTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: SourceId
    account_id: SourceId
    posted_on: date
    amount: Annotated[Money, BeforeValidator(_require_string)]
    currency: CurrencyCode
    description: Annotated[str, StringConstraints(max_length=500)]
    vendor: Label
    category: Label
    categorized_at: AwareDatetime
    """When the source made this categorization: the version time. Not the relay's
    `created_at`, which is when the relay received it and can invert order after retries."""


class TransactionCategorized(BaseModel):
    """The `data` of a `transaction.categorized` event."""

    model_config = ConfigDict(frozen=True)

    entity_id: SourceId
    transaction: CategorizedTransaction

    def to_transaction(self) -> Transaction:
        t = self.transaction
        return Transaction(
            entity_id=self.entity_id,
            source_id=t.id,
            account_id=t.account_id,
            posted_on=t.posted_on,
            amount=t.amount,
            description=t.description,
            vendor=t.vendor,
            category=t.category,
        )
