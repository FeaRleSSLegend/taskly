"""initial schema: users, roadmaps, nodes, node_dependencies

Revision ID: 0001
Revises:
Create Date: 2026-08-31

"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

roadmap_type = sa.Enum("sequential", "flat", name="roadmap_type")
roadmap_status = sa.Enum(
    "pending", "generating_phases", "generating_tasks", "done", "failed",
    name="roadmap_status",
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "roadmaps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("goal_text", sa.Text(), nullable=False),
        sa.Column("type", roadmap_type, nullable=False),
        sa.Column("status", roadmap_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_roadmaps_user_id", "roadmaps", ["user_id"])

    op.create_table(
        "nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("roadmap_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("time_estimate", sa.String(length=100), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["roadmap_id"], ["roadmaps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nodes_roadmap_id", "nodes", ["roadmap_id"])

    op.create_table(
        "node_dependencies",
        sa.Column("depends_on_node_id", sa.Uuid(), nullable=False),
        sa.Column("dependent_node_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on_node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dependent_node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("depends_on_node_id", "dependent_node_id"),
        sa.UniqueConstraint(
            "depends_on_node_id", "dependent_node_id", name="uq_node_dependency"
        ),
    )


def downgrade() -> None:
    op.drop_table("node_dependencies")
    op.drop_index("ix_nodes_roadmap_id", table_name="nodes")
    op.drop_table("nodes")
    op.drop_index("ix_roadmaps_user_id", table_name="roadmaps")
    op.drop_table("roadmaps")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    roadmap_status.drop(op.get_bind(), checkfirst=True)
    roadmap_type.drop(op.get_bind(), checkfirst=True)
