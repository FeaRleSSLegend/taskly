"""add chat_messages table

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-11

"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL needs the enum type created explicitly before the column can
    # reference it. op.create_table with sa.Enum creates it automatically on
    # Postgres and is a no-op for SQLite (which uses VARCHAR for enums).
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("user", "assistant", "tool", name="chat_role"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chat_messages_user_id"), "chat_messages", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_messages_user_id"), table_name="chat_messages")
    op.drop_table("chat_messages")
    # Drop the enum type on PostgreSQL; this is a no-op on SQLite.
    op.execute("DROP TYPE IF EXISTS chat_role")
