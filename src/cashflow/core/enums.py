"""Domain vocabularies. Persisted as their string values."""

from enum import StrEnum


class Cadence(StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class AnomalyType(StrEnum):
    DUPLICATE_CHARGE = "duplicate_charge"
    CATEGORY_SPIKE = "category_spike"
    NEW_VENDOR_LARGE = "new_vendor_large"
    MISSED_RECURRING = "missed_recurring"
    RECURRING_AMOUNT_CHANGE = "recurring_amount_change"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnomalyStatus(StrEnum):
    OPEN = "open"
    DISMISSED = "dismissed"
