"""Name that Frame standings (#21): per-user, per-difficulty streaks.

Revision ID: e8a41f5c9d27
Revises: c31d9e6f2a41
Create Date: 2026-08-20 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e8a41f5c9d27"
down_revision = "c31d9e6f2a41"
branch_labels = None
depends_on = None


def upgrade():
    """Create user_frame_score: the running streak and personal best,
    one row per (user, difficulty)."""

    op.create_table(
        "user_frame_score",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("difficulty", sa.String(length=16), nullable=False),
        sa.Column("current_streak", sa.Integer(), nullable=False),
        sa.Column("best_streak", sa.Integer(), nullable=False),
        sa.Column("date_best", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "difficulty"),
    )
    op.create_index(
        op.f("ix_user_frame_score_user_id"), "user_frame_score", ["user_id"]
    )


def downgrade():
    """Drop the standings table."""

    op.drop_index(op.f("ix_user_frame_score_user_id"), table_name="user_frame_score")
    op.drop_table("user_frame_score")
