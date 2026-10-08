"""track inbound event outcome and transaction versions

Revision ID: 766121ef3d75
Revises: d83450c5726c
Create Date: 2026-10-08 14:37:11.026727

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "766121ef3d75"
down_revision: str | Sequence[str] | None = "d83450c5726c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STATUSES = ("received", "processed", "ignored", "invalid", "unknown_entity")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "inbound_events",
        sa.Column(
            "status",
            sa.Enum(
                *STATUSES,
                name="inbound_event_status",
                native_enum=False,
                create_constraint=False,  # declared explicitly below, as in the first revision
                length=32,
            ),
            server_default="received",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_inbound_events_inbound_event_status"),
        "inbound_events",
        sa.column("status").in_(STATUSES),
    )
    op.execute("UPDATE inbound_events SET status = 'processed' WHERE processed_at IS NOT NULL")
    op.add_column("inbound_events", sa.Column("entity_id", sa.String(length=64), nullable=True))
    op.add_column("inbound_events", sa.Column("error", sa.Text(), nullable=True))
    op.create_index(
        "ix_inbound_events_entity_status", "inbound_events", ["entity_id", "status"], unique=False
    )

    # Existing rows have no upstream version time; their insert time is the best available.
    op.add_column(
        "transactions", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE transactions SET source_updated_at = created_at")
    op.alter_column("transactions", "source_updated_at", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("transactions", "source_updated_at")
    op.drop_index("ix_inbound_events_entity_status", table_name="inbound_events")
    op.drop_column("inbound_events", "error")
    op.drop_column("inbound_events", "entity_id")
    op.drop_constraint(
        op.f("ck_inbound_events_inbound_event_status"), "inbound_events", type_="check"
    )
    op.drop_column("inbound_events", "status")
