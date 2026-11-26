"""rename users.role value 'instructor' to 'lab manager'

Revision ID: 0008_rename_instructor_role
Revises: 0007_add_deploying_status
Create Date: 2026-11-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_rename_instructor_role"
down_revision: str | Sequence[str] | None = "0007_add_deploying_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

users = sa.table("users", sa.column("role", sa.String))


def upgrade() -> None:
    op.execute(users.update().where(users.c.role == "instructor").values(role="lab manager"))


def downgrade() -> None:
    op.execute(users.update().where(users.c.role == "lab manager").values(role="instructor"))
