"""Application settings, loaded from the environment (and `.env` in development).

Nested groups are set with a double-underscore delimiter, e.g.
`ANOMALY__DUPLICATE_WINDOW_DAYS=3`.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import BaseModel, Field, PositiveInt, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cashflow.core.enums import Cadence, ForecastModel

Fraction = Annotated[Decimal, Field(gt=0, lt=1)]


class WebhookSettings(BaseModel):
    """Inbound relay webhook verification."""

    secret: SecretStr = SecretStr("")
    signature_tolerance_seconds: PositiveInt = 300
    max_body_bytes: PositiveInt = 256 * 1024


DEFAULT_MIN_OCCURRENCES: dict[Cadence, int] = {
    Cadence.WEEKLY: 4,
    Cadence.BIWEEKLY: 3,
    Cadence.MONTHLY: 3,
    Cadence.QUARTERLY: 3,
    # Three annual charges would need three years of history; two is enough evidence.
    Cadence.ANNUAL: 2,
}


class RecurringSettings(BaseModel):
    """Recurring-series detection. See "Recurring detection" in docs/ARCHITECTURE.md."""

    min_occurrences: dict[Cadence, Annotated[int, Field(ge=2)]] = Field(
        default_factory=lambda: dict(DEFAULT_MIN_OCCURRENCES)
    )
    amount_tolerance: Fraction = Decimal("0.10")
    cadence_tolerance_days: PositiveInt = 3
    min_consistent_share: Annotated[float, Field(gt=0, le=1)] = 0.75
    """Share of amounts, and of gaps between charges, that must fit the series."""
    max_missed_cycles: PositiveInt = 3
    """A series that has missed more expected charges than this is treated as cancelled."""

    @field_validator("min_occurrences")
    @classmethod
    def _fill_unset_cadences(cls, value: dict[Cadence, int]) -> dict[Cadence, int]:
        # Overriding one cadence (e.g. via env JSON) keeps the defaults for the others.
        return {**DEFAULT_MIN_OCCURRENCES, **value}


class ForecastSettings(BaseModel):
    """Horizons, candidate models, and backtesting. See "Forecasting" in ARCHITECTURE.md."""

    horizons_days: tuple[PositiveInt, ...] = (30, 60, 90)
    confidence_level: Annotated[int, Field(gt=0, lt=100)] = 80
    # SeasonalNaive is available but not a default: copying last week's flows compounds into
    # large balance errors (39% median at 90 days on the demo data vs 6% for these two).
    candidate_models: tuple[ForecastModel, ...] = (
        ForecastModel.AUTO_ETS,
        ForecastModel.HISTORIC_AVERAGE,
    )
    fallback_model: ForecastModel = ForecastModel.HISTORIC_AVERAGE
    """Used when history is too short to backtest and choose."""
    backtest_windows: PositiveInt = 3
    backtest_step_days: PositiveInt = 30
    min_training_days: PositiveInt = 180
    """Each backtest window must have at least this much history before its cutoff."""

    @property
    def max_horizon(self) -> int:
        return max(self.horizons_days)


class AnomalySettings(BaseModel):
    """Thresholds for the anomaly rules in ARCHITECTURE.md."""

    duplicate_window_days: PositiveInt = 3
    category_spike_z: Annotated[float, Field(gt=0)] = 3.5
    category_baseline_periods: PositiveInt = 6
    new_vendor_large_amount: Annotated[Decimal, Field(gt=0)] = Decimal("1000.00")
    missed_recurring_grace_days: PositiveInt = 5
    recurring_amount_tolerance: Fraction = Decimal("0.15")


class Settings(BaseSettings):
    """Root settings object. Build one with `get_settings()` or inject your own in tests."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://cashflow:cashflow@localhost:5432/cashflow"
    )
    database_echo: bool = False

    webhook: WebhookSettings = WebhookSettings()
    recurring: RecurringSettings = RecurringSettings()
    forecast: ForecastSettings = ForecastSettings()
    anomaly: AnomalySettings = AnomalySettings()


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once. Tests should construct `Settings` directly instead."""
    return Settings()
