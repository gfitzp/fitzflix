"""Replace the stored Convert HDR to SDR flag with the HDR import mode (#657)

The setting HDR_IMPORT_MODE replaces the flag CONVERT_HDR_TO_SDR. A
Settings page can have a stored value of the flag. A stored "off"
becomes the mode "keep". A stored "on" is the default mode, so its row
goes away. The step changes only rows of app_setting. Thus, it can run
before or after the restart.

Revision ID: a4c7e2b9d013
Revises: e5d2a8c47f13
Create Date: 2026-11-06 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a4c7e2b9d013"
down_revision = "e5d2a8c47f13"
branch_labels = None
depends_on = None

FLAG = "CONVERT_HDR_TO_SDR"
MODE = "HDR_IMPORT_MODE"


def _value(connection, name):
    """Return the stored text of a setting, or None if it has no row."""

    row = connection.execute(
        sa.text("SELECT value FROM app_setting WHERE name = :name"), {"name": name}
    ).first()
    return row[0] if row else None


def _rename(connection, old, new, value):
    """Give the row of a setting a new name and a new value."""

    connection.execute(
        sa.text("UPDATE app_setting SET name = :new, value = :value WHERE name = :old"),
        {"old": old, "new": new, "value": value},
    )


def _delete(connection, name):
    """Remove the row of a setting."""

    connection.execute(
        sa.text("DELETE FROM app_setting WHERE name = :name"), {"name": name}
    )


def flag_to_mode(connection):
    """Change a stored flag to a stored mode. A stored mode has priority."""

    flag = _value(connection, FLAG)
    if flag == "0" and _value(connection, MODE) is None:
        _rename(connection, FLAG, MODE, "keep")
    elif flag is not None:
        _delete(connection, FLAG)


def mode_to_flag(connection):
    """Change a stored mode back to a stored flag.

    Only "keep" has a flag value, which is "off". Each mode that
    converts becomes the default of the flag, which is "on"."""

    mode = _value(connection, MODE)
    if mode == "keep" and _value(connection, FLAG) is None:
        _rename(connection, MODE, FLAG, "0")
    elif mode is not None:
        _delete(connection, MODE)


def upgrade():
    flag_to_mode(op.get_bind())


def downgrade():
    mode_to_flag(op.get_bind())
