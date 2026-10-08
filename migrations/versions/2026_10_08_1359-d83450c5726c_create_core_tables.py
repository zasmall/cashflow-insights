"""create core tables

Revision ID: d83450c5726c
Revises:
Create Date: 2026-10-08 13:59:26.489202

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d83450c5726c"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Enum CHECKs are declared once, explicitly, as ck_<table>_<enum>; the sa.Enum columns
    # use create_constraint=False so they don't emit a second unnamed copy.
    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("opening_balance", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("opening_balance_on", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_entities_currency_iso")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
    )
    op.create_table(
        "inbound_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("relay_event_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_events")),
        sa.UniqueConstraint("relay_event_id", name=op.f("uq_inbound_events_relay_event_id")),
    )
    op.create_table(
        "anomalies",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "duplicate_charge",
                "category_spike",
                "new_vendor_large",
                "missed_recurring",
                "recurring_amount_change",
                name="anomaly_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum(
                "low",
                "medium",
                "high",
                name="severity",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column(
            "transaction_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "dismissed",
                name="anomaly_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'dismissed') = (dismissed_at IS NOT NULL)",
            name=op.f("ck_anomalies_dismissed_at_matches"),
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')", name=op.f("ck_anomalies_severity")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'dismissed')", name=op.f("ck_anomalies_anomaly_status")
        ),
        sa.CheckConstraint(
            "type IN ('duplicate_charge', 'category_spike', 'new_vendor_large', 'missed_recurring', 'recurring_amount_change')",
            name=op.f("ck_anomalies_anomaly_type"),
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_anomalies_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomalies")),
        sa.UniqueConstraint("entity_id", "fingerprint", name="uq_anomalies_entity_fingerprint"),
    )
    op.create_index(
        "ix_anomalies_entity_status", "anomalies", ["entity_id", "status"], unique=False
    )
    op.create_table(
        "forecasts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=50), nullable=False),
        sa.Column("backtest_mase", sa.Double(), nullable=True),
        sa.CheckConstraint("horizon_days > 0", name=op.f("ck_forecasts_horizon_positive")),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_forecasts_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecasts")),
    )
    op.create_index(
        "ix_forecasts_entity_generated_at", "forecasts", ["entity_id", "generated_at"], unique=False
    )
    op.create_table(
        "recurring_series",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("vendor", sa.String(length=200), nullable=False),
        sa.Column("typical_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "cadence",
            sa.Enum(
                "weekly",
                "biweekly",
                "monthly",
                "quarterly",
                "annual",
                name="cadence",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("next_expected_on", sa.Date(), nullable=False),
        sa.Column("last_seen_on", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cadence IN ('weekly', 'biweekly', 'monthly', 'quarterly', 'annual')",
            name=op.f("ck_recurring_series_cadence"),
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_recurring_series_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_series")),
    )
    op.create_index(
        op.f("ix_recurring_series_entity_id"), "recurring_series", ["entity_id"], unique=False
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("vendor", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_transactions_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint("entity_id", "source_id", name="uq_transactions_entity_source"),
    )
    op.create_index(
        "ix_transactions_entity_category_posted_on",
        "transactions",
        ["entity_id", "category", "posted_on"],
        unique=False,
    )
    op.create_index(
        "ix_transactions_entity_posted_on", "transactions", ["entity_id", "posted_on"], unique=False
    )
    op.create_index(
        "ix_transactions_entity_vendor", "transactions", ["entity_id", "vendor"], unique=False
    )
    op.create_table(
        "forecast_points",
        sa.Column("forecast_id", sa.BigInteger(), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("expected_balance", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("lower", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("upper", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.CheckConstraint(
            "lower <= expected_balance AND expected_balance <= upper",
            name=op.f("ck_forecast_points_band"),
        ),
        sa.ForeignKeyConstraint(
            ["forecast_id"],
            ["forecasts.id"],
            name=op.f("fk_forecast_points_forecast_id_forecasts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("forecast_id", "on_date", name=op.f("pk_forecast_points")),
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("forecast_points")
    op.drop_index("ix_transactions_entity_vendor", table_name="transactions")
    op.drop_index("ix_transactions_entity_posted_on", table_name="transactions")
    op.drop_index("ix_transactions_entity_category_posted_on", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(op.f("ix_recurring_series_entity_id"), table_name="recurring_series")
    op.drop_table("recurring_series")
    op.drop_index("ix_forecasts_entity_generated_at", table_name="forecasts")
    op.drop_table("forecasts")
    op.drop_index("ix_anomalies_entity_status", table_name="anomalies")
    op.drop_table("anomalies")
    op.drop_table("inbound_events")
    op.drop_table("entities")
    # ### end Alembic commands ###
