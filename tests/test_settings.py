from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow.settings import Settings


def test_defaults_match_architecture(settings: Settings) -> None:
    assert settings.forecast.horizons_days == (30, 60, 90)
    assert settings.webhook.signature_tolerance_seconds == 300
    assert settings.database_url.scheme == "postgresql+psycopg"


def test_money_thresholds_are_decimal(settings: Settings) -> None:
    assert isinstance(settings.anomaly.new_vendor_large_amount, Decimal)
    assert isinstance(settings.recurring.amount_tolerance, Decimal)


def test_nested_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOMALY__DUPLICATE_WINDOW_DAYS", "7")
    monkeypatch.setenv("ANOMALY__NEW_VENDOR_LARGE_AMOUNT", "2500.50")
    monkeypatch.setenv("WEBHOOK__SECRET", "s3cret")

    settings = Settings(_env_file=None)

    assert settings.anomaly.duplicate_window_days == 7
    assert settings.anomaly.new_vendor_large_amount == Decimal("2500.50")
    assert settings.webhook.secret.get_secret_value() == "s3cret"


def test_secret_is_not_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK__SECRET", "s3cret")

    assert "s3cret" not in repr(Settings(_env_file=None))


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("RECURRING__AMOUNT_TOLERANCE", "1.5"),
        ("ANOMALY__DUPLICATE_WINDOW_DAYS", "0"),
        ("DATABASE_URL", "mysql://nope"),
    ],
)
def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
