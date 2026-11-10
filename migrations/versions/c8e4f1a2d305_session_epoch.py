"""Add the session epoch to the user table (#673)

The session id, the remember cookie and the reset token carry a
fingerprint of the password hash and this epoch. A change of the
sign-in methods gives a new epoch. The step gives each user a random
epoch. Thus, each session from before the step stops loading, and each
member signs in 1 time again. Run it before the restart: the new code
reads the column.

Revision ID: c8e4f1a2d305
Revises: b7c3d9e1f204
Create Date: 2026-11-10 15:00:00.000000

"""

import secrets

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8e4f1a2d305"
down_revision = "b7c3d9e1f204"
branch_labels = None
depends_on = None


USER = sa.table("user", sa.column("id"), sa.column("session_epoch"))


def give_each_user_an_epoch(connection):
    """Give each user row its own random session epoch."""

    for (user_id,) in connection.execute(sa.select(USER.c.id)).all():
        connection.execute(
            USER.update()
            .where(USER.c.id == user_id)
            .values(session_epoch=secrets.token_hex(16))
        )


def upgrade():
    op.add_column(
        "user", sa.Column("session_epoch", sa.String(length=32), nullable=True)
    )
    give_each_user_an_epoch(op.get_bind())


def downgrade():
    op.drop_column("user", "session_epoch")
