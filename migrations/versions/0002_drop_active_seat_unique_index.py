"""drop active-seat unique index (Stage 2: intentionally unsafe demo)

The naive booking implementation added in this stage is a plain
"SELECT is it free? then INSERT" with no DB-level backstop. Dropping the
partial unique index here removes the only thing that was stopping it from
double-booking, so the race-condition demo in scripts/naive_race_test.py can
actually reproduce the bug. Migration 0003 (Stage 3) re-creates this index as
the fix.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_bookings_active_seat")


def downgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_bookings_active_seat
            ON bookings(seat_id)
            WHERE status IN ('held','pending_payment','confirmed')
        """
    )
