"""Add the passkey table (#669)

A passkey is a WebAuthn credential of a user. The table holds the
public key, the credential id that a sign-in names, the sign count,
the transports, a name, and the dates. The step adds 1 new table. It
touches no other table. Thus, it can run before or after the restart.

Revision ID: b7c3d9e1f204
Revises: a4c7e2b9d013
Create Date: 2026-11-07 20:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c3d9e1f204"
down_revision = "a4c7e2b9d013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "passkey",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.VARBINARY(1023), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("transports", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("date_created", sa.DateTime(), nullable=False),
        sa.Column("date_last_used", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id", name="uq_passkey_credential_id"),
    )
    op.create_index("ix_passkey_user_id", "passkey", ["user_id"])


def downgrade():
    op.drop_index("ix_passkey_user_id", table_name="passkey")
    op.drop_table("passkey")
