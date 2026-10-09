"""recurring series projected amount

Revision ID: c479471f3671
Revises: 336f97ccb60b
Create Date: 2026-10-08 19:20:39.663192

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c479471f3671"
down_revision: str | Sequence[str] | None = "336f97ccb60b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Existing series have no confirmed price change yet: project them at typical_amount
    until the next refresh recomputes them."""
    op.add_column(
        "recurring_series",
        sa.Column("projected_amount", sa.Numeric(precision=14, scale=2), nullable=True),
    )
    op.execute("UPDATE recurring_series SET projected_amount = typical_amount")
    op.alter_column("recurring_series", "projected_amount", nullable=False)


def downgrade() -> None:
    op.drop_column("recurring_series", "projected_amount")
