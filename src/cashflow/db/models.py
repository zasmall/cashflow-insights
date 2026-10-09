"""ORM models for the tables in docs/ARCHITECTURE.md.

Alembic's env.py imports this module so every model is registered on `Base.metadata`.
Callers that also use `cashflow.core.models` should import this module as `orm`.
"""

from datetime import date, datetime
from typing import Annotated, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cashflow.core.enums import AnomalyStatus, AnomalyType, Cadence, InboundEventStatus, Severity
from cashflow.db.base import Base, BigIntPk, Money, SourceId, TimestampMixin, str_enum

EntityFk = Annotated[str, mapped_column(String(64), ForeignKey("entities.id", ondelete="CASCADE"))]


class Entity(TimestampMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),)

    id: Mapped[SourceId] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3))
    opening_balance: Mapped[Money]
    opening_balance_on: Mapped[date]
    # Set when new transactions arrive; cleared when a refresh claims the entity.
    dirty_since: Mapped[datetime | None]


class InboundEvent(Base):
    """Dedupe log for relay deliveries, which arrive at least once."""

    __tablename__ = "inbound_events"
    __table_args__ = (Index("ix_inbound_events_entity_status", "entity_id", "status"),)

    id: Mapped[BigIntPk]
    relay_event_id: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[InboundEventStatus] = mapped_column(
        str_enum(InboundEventStatus, "inbound_event_status"),
        server_default=InboundEventStatus.RECEIVED.value,
    )
    # No FK: events for entities not provisioned yet are kept for later reprocessing.
    entity_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]


class Transaction(TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("entity_id", "source_id", name="uq_transactions_entity_source"),
        Index("ix_transactions_entity_posted_on", "entity_id", "posted_on"),
        Index("ix_transactions_entity_vendor", "entity_id", "vendor"),
        Index("ix_transactions_entity_category_posted_on", "entity_id", "category", "posted_on"),
    )

    id: Mapped[BigIntPk]
    entity_id: Mapped[EntityFk]
    source_id: Mapped[SourceId]
    account_id: Mapped[SourceId]
    posted_on: Mapped[date]
    amount: Mapped[Money]
    description: Mapped[str] = mapped_column(String(500))
    vendor: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(200))
    # When upstream emitted this version. Upserts never replace a newer version with an older
    # one, so a delayed retry of an old event can't undo a later recategorization.
    source_updated_at: Mapped[datetime]


class RecurringSeries(TimestampMixin, Base):
    """A detected bill or income stream. Recomputed per entity, so it has no natural key."""

    __tablename__ = "recurring_series"

    id: Mapped[BigIntPk]
    entity_id: Mapped[EntityFk] = mapped_column(index=True)
    vendor: Mapped[str] = mapped_column(String(200))
    typical_amount: Mapped[Money]
    # The amount to expect next: typical_amount, or a new price confirmed by recent charges.
    projected_amount: Mapped[Money]
    cadence: Mapped[Cadence] = mapped_column(str_enum(Cadence, "cadence"))
    next_expected_on: Mapped[date]
    last_seen_on: Mapped[date]
    anchor_day: Mapped[int | None] = mapped_column(SmallInteger)


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (
        CheckConstraint("horizon_days > 0", name="horizon_positive"),
        Index("ix_forecasts_entity_generated_at", "entity_id", "generated_at"),
    )

    id: Mapped[BigIntPk]
    entity_id: Mapped[EntityFk]
    generated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    horizon_days: Mapped[int]
    as_of: Mapped[date]
    starting_balance: Mapped[Money]
    model: Mapped[str] = mapped_column(String(50))
    # Model-quality metrics, not money, so float is appropriate. Null when history is too short.
    backtest_mase: Mapped[float | None]
    backtest_coverage: Mapped[float | None]
    backtest_balance_error: Mapped[Money | None]

    points: Mapped[list["ForecastPoint"]] = relationship(
        back_populates="forecast",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ForecastPoint.on_date",
    )


class ForecastPoint(Base):
    __tablename__ = "forecast_points"
    __table_args__ = (
        CheckConstraint("lower <= expected_balance AND expected_balance <= upper", name="band"),
    )

    forecast_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("forecasts.id", ondelete="CASCADE"), primary_key=True
    )
    on_date: Mapped[date] = mapped_column(primary_key=True)
    expected_balance: Mapped[Money]
    lower: Mapped[Money]
    upper: Mapped[Money]

    forecast: Mapped[Forecast] = relationship(back_populates="points")


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        # One row per finding, open or dismissed, so rescans never duplicate or resurrect it.
        UniqueConstraint("entity_id", "fingerprint", name="uq_anomalies_entity_fingerprint"),
        CheckConstraint(
            "(status = 'dismissed') = (dismissed_at IS NOT NULL)", name="dismissed_at_matches"
        ),
        Index("ix_anomalies_entity_status", "entity_id", "status"),
    )

    id: Mapped[BigIntPk]
    entity_id: Mapped[EntityFk]
    type: Mapped[AnomalyType] = mapped_column(str_enum(AnomalyType, "anomaly_type"))
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "severity"))
    explanation: Mapped[str] = mapped_column(Text)
    transaction_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=text("'{}'")
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[AnomalyStatus] = mapped_column(
        str_enum(AnomalyStatus, "anomaly_status"), server_default=AnomalyStatus.OPEN.value
    )
    dismissed_at: Mapped[datetime | None]
