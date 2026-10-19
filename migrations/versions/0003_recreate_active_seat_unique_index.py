"""recreate active-seat unique index (Stage 3 fix)

Restores uq_bookings_active_seat, which migration 0002 dropped to let the
Stage 2 naive implementation double-book. Strategy A (app/bookings/repository.py)
depends on this index existing: it's what turns a losing concurrent insert
into a unique-violation it can catch and report as 409.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Stage 2's own race-proof script left genuine duplicate active bookings
    # on seat_id=1 (that was the point). You cannot retroactively add a
    # uniqueness constraint over data that already violates it, so resolve
    # existing violations first: keep the earliest active booking per seat,
    # expire the rest. In a real migration against production data this is
    # the same move -- decide a tie-break rule and clean up before the
    # constraint goes on, not after.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, seat_id,
                   row_number() OVER (PARTITION BY seat_id ORDER BY created_at, id) AS rn
            FROM bookings
            WHERE status IN ('held', 'pending_payment', 'confirmed')
        )
        UPDATE bookings
        SET status = 'expired', updated_at = now()
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_bookings_active_seat
            ON bookings(seat_id)
            WHERE status IN ('held','pending_payment','confirmed')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_bookings_active_seat")
