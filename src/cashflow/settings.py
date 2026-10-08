"""Application settings, loaded from the environment (and `.env` in development).

Nested groups are set with a double-underscore delimiter, e.g.
`ANOMALY__DUPLICATE_WINDOW_DAYS=3`.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import BaseModel, Field, PositiveInt, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Fraction = Annotated[Decimal, Field(gt=0, lt=1)]


class WebhookSettings(BaseModel):
    """Inbound relay webhook verification."""

    secret: SecretStr = SecretStr("")
    signature_tolerance_seconds: PositiveInt = 300


class RecurringSettings(BaseModel):
    """Recurring-series detection."""

    min_occurrences: Annotated[int, Field(ge=2)] = 3
    amount_tolerance: Fraction = Decimal("0.10")
    cadence_tolerance_days: PositiveInt = 3


class ForecastSettings(BaseModel):
    """Forecast horizons and backtesting."""

    horizons_days: tuple[PositiveInt, ...] = (30, 60, 90)
    confidence_level: Annotated[int, Field(gt=0, lt=100)] = 80
    backtest_windows: PositiveInt = 3


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
