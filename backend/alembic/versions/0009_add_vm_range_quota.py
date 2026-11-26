"""add vm_quota and range_quota to sessions

Revision ID: 0009_add_vm_range_quota
Revises: 0008_rename_instructor_role
Create Date: 2026-11-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_add_vm_range_quota"
down_revision: str | Sequence[str] | None = "0008_rename_instructor_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("vm_quota", sa.Integer(), nullable=True))
    op.add_column("sessions", sa.Column("range_quota", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "range_quota")
    op.drop_column("sessions", "vm_quota")
