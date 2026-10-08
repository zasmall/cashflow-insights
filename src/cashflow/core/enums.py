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


class InboundEventStatus(StrEnum):
    """What became of a relay delivery. Every terminal status is answered with 2xx."""

    RECEIVED = "received"
    """Recorded but not yet handled. Ingest finishes in the same transaction, so never committed."""
    PROCESSED = "processed"
    IGNORED = "ignored"
    """An event type we don't consume."""
    INVALID = "invalid"
    """Signed by the relay but unusable; `error` says why. Retrying can't fix it."""
    UNKNOWN_ENTITY = "unknown_entity"
    """For an entity not provisioned here yet. Kept so it can be reprocessed later."""


class ForecastModel(StrEnum):
    """Candidate models for residual (non-recurring) daily flow. Each produces intervals."""

    AUTO_ETS = "AutoETS"
    SEASONAL_NAIVE = "SeasonalNaive"
    HISTORIC_AVERAGE = "HistoricAverage"
