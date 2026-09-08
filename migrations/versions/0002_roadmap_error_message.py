"""add roadmaps.error_message

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roadmaps", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("roadmaps", "error_message")
