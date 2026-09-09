"""add nodes.phase

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09

"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("phase", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "phase")
