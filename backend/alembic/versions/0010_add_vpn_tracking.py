"""add vpn tracking columns to students

Revision ID: 0010_add_vpn_tracking
Revises: 0009_add_vm_range_quota
Create Date: 2026-11-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_add_vpn_tracking"
down_revision: str | Sequence[str] | None = "0009_add_vm_range_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("students", sa.Column("vpn_ip", sa.String(length=64), nullable=True))
    op.add_column(
        "students",
        sa.Column("vpn_last_seen", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("students", "vpn_last_seen")
    op.drop_column("students", "vpn_ip")
