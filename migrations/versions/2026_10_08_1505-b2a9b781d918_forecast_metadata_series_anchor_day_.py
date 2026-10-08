"""forecast metadata, series anchor day, entity dirty flag

Revision ID: b2a9b781d918
Revises: 766121ef3d75
Create Date: 2026-10-08 15:05:21.524526

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2a9b781d918"
down_revision: str | Sequence[str] | None = "766121ef3d75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("entities", sa.Column("dirty_since", sa.DateTime(timezone=True), nullable=True))
    # Forecasts are derived data that the next refresh regenerates, so clear any old rows rather
    # than inventing values for the new NOT NULL columns.
    op.execute("DELETE FROM forecasts")
    op.add_column("forecasts", sa.Column("as_of", sa.Date(), nullable=False))
    op.add_column(
        "forecasts",
        sa.Column("starting_balance", sa.Numeric(precision=14, scale=2), nullable=False),
    )
    op.add_column("forecasts", sa.Column("backtest_coverage", sa.Double(), nullable=True))
    op.add_column(
        "forecasts",
        sa.Column("backtest_balance_error", sa.Numeric(precision=14, scale=2), nullable=True),
    )
    op.add_column("recurring_series", sa.Column("anchor_day", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("recurring_series", "anchor_day")
    op.drop_column("forecasts", "backtest_balance_error")
    op.drop_column("forecasts", "backtest_coverage")
    op.drop_column("forecasts", "starting_balance")
    op.drop_column("forecasts", "as_of")
    op.drop_column("entities", "dirty_since")
