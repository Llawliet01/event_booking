"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id            BIGSERIAL PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE events (
            id         BIGSERIAL PRIMARY KEY,
            name       TEXT NOT NULL,
            starts_at  TIMESTAMPTZ NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE seats (
            id        BIGSERIAL PRIMARY KEY,
            event_id  BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            label     TEXT NOT NULL,
            UNIQUE (event_id, label)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE bookings (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            seat_id         BIGINT NOT NULL REFERENCES seats(id) ON DELETE RESTRICT,
            status          TEXT NOT NULL CHECK (status IN ('held','pending_payment','confirmed','expired','cancelled')),
            hold_expires_at TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_hold_expiry_required
                CHECK (status NOT IN ('held', 'pending_payment') OR hold_expires_at IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_bookings_active_seat
            ON bookings(seat_id)
            WHERE status IN ('held','pending_payment','confirmed')
        """
    )
    op.execute("CREATE INDEX idx_bookings_user_id ON bookings(user_id)")
    op.execute(
        """
        CREATE INDEX idx_bookings_seat_status_expiry
            ON bookings(seat_id, status, hold_expires_at)
            WHERE status IN ('held','pending_payment')
        """
    )

    op.execute(
        """
        CREATE TABLE idempotency_keys (
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key           TEXT NOT NULL,
            request_hash  TEXT NOT NULL,
            status        TEXT NOT NULL CHECK (status IN ('in_progress','completed')),
            response_code INT,
            response_body JSONB,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, key)
        )
        """
    )
    op.execute("CREATE INDEX idx_idempotency_created_at ON idempotency_keys(created_at)")

    op.execute(
        """
        CREATE TABLE webhook_events (
            event_id    TEXT PRIMARY KEY,
            payload     JSONB NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE payments (
            id               BIGSERIAL PRIMARY KEY,
            booking_id       BIGINT NOT NULL REFERENCES bookings(id) ON DELETE RESTRICT,
            webhook_event_id TEXT NOT NULL UNIQUE REFERENCES webhook_events(event_id) ON DELETE RESTRICT,
            amount_cents     BIGINT NOT NULL CHECK (amount_cents >= 0),
            status           TEXT NOT NULL CHECK (status IN ('succeeded','failed')),
            needs_review     BOOLEAN NOT NULL DEFAULT false,
            refund_status    TEXT NOT NULL DEFAULT 'not_required'
                                 CHECK (refund_status IN ('not_required','pending','refunded')),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_payments_booking_id ON payments(booking_id)")
    op.execute(
        "CREATE INDEX idx_payments_needs_review ON payments(needs_review) WHERE needs_review = true"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS payments")
    op.execute("DROP TABLE IF EXISTS webhook_events")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
    op.execute("DROP TABLE IF EXISTS bookings")
    op.execute("DROP TABLE IF EXISTS seats")
    op.execute("DROP TABLE IF EXISTS events")
    op.execute("DROP TABLE IF EXISTS users")
