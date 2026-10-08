"""Domain models shared by ingest, the demo generator, and persistence."""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]
"""A currency amount with exactly cent precision. Negative means an outflow."""

SourceId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
"""An identifier assigned upstream. Opaque: never parse or assume a format."""

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Entity(BaseModel):
    """A client business. Forecasts start from its opening balance."""

    model_config = ConfigDict(frozen=True)

    id: SourceId
    name: Label
    currency: CurrencyCode
    opening_balance: Money
    opening_balance_on: date


class Transaction(BaseModel):
    """A categorized bank transaction for one entity."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    entity_id: SourceId
    source_id: SourceId
    account_id: SourceId
    posted_on: date
    amount: Money
    description: Annotated[str, StringConstraints(max_length=500)]
    vendor: Label
    category: Label
