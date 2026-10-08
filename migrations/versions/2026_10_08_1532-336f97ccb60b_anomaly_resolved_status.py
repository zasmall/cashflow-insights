"""anomaly resolved status

Revision ID: 336f97ccb60b
Revises: b2a9b781d918
Create Date: 2026-10-08 15:32:28.087932

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "336f97ccb60b"
down_revision: str | Sequence[str] | None = "b2a9b781d918"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSTRAINT = op.f("ck_anomalies_anomaly_status")  # already final; skip the naming convention


def upgrade() -> None:
    """Allow 'resolved', set by rescans when a missed bill arrives."""
    op.drop_constraint(CONSTRAINT, "anomalies", type_="check")
    op.create_check_constraint(
        CONSTRAINT, "anomalies", sa.column("status").in_(("open", "dismissed", "resolved"))
    )


def downgrade() -> None:
    """Resolved anomalies reopen, as there is no 'resolved' to keep them in."""
    op.execute("UPDATE anomalies SET status = 'open' WHERE status = 'resolved'")
    op.drop_constraint(CONSTRAINT, "anomalies", type_="check")
    op.create_check_constraint(
        CONSTRAINT, "anomalies", sa.column("status").in_(("open", "dismissed"))
    )
