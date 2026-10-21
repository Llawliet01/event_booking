"""Payment webhook processing: dedupe, record the payment, and confirm the
booking -- all in one transaction. A crash anywhere in here rolls back
everything, including the webhook_events dedupe row, so a provider retry
(real providers retry on a non-2xx response) replays cleanly instead of
landing in a half-applied state.

If the hold had already lapsed by the time a 'succeeded' event arrives,
the booking is deliberately NOT confirmed -- the seat may have already
been re-sold to someone else. The payment is recorded with needs_review
and refund_status='pending' instead, for a human (or
app/bookings/reconciliation.py, for the crash variant of this same
problem) to act on.
"""
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


class BookingNotFoundError(Exception):
    pass


async def process_payment_webhook(
    conn: AsyncConnection,
    event_id: str,
    payload: dict,
    booking_id: int,
    amount_cents: int,
    status: str,
) -> None:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO webhook_events (event_id, payload)
                VALUES (%(event_id)s, %(payload)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                {"event_id": event_id, "payload": Jsonb(payload)},
            )
            if await cur.fetchone() is None:
                # Already processed this exact event (provider retry, or a
                # duplicate delivery) -- no-op, commit, done.
                return

            await cur.execute("SELECT 1 FROM bookings WHERE id = %(id)s", {"id": booking_id})
            if await cur.fetchone() is None:
                raise BookingNotFoundError()

            if status == "succeeded":
                await cur.execute(
                    """
                    SELECT 1 FROM bookings
                    WHERE id = %(id)s AND status = 'pending_payment' AND hold_expires_at > now()
                    """,
                    {"id": booking_id},
                )
                still_valid = await cur.fetchone() is not None

                await cur.execute(
                    """
                    INSERT INTO payments
                        (booking_id, webhook_event_id, amount_cents, status, needs_review, refund_status)
                    VALUES
                        (%(booking_id)s, %(event_id)s, %(amount_cents)s, 'succeeded',
                         %(needs_review)s, %(refund_status)s)
                    """,
                    {
                        "booking_id": booking_id,
                        "event_id": event_id,
                        "amount_cents": amount_cents,
                        "needs_review": not still_valid,
                        "refund_status": "not_required" if still_valid else "pending",
                    },
                )

                await cur.execute(
                    """
                    UPDATE bookings
                    SET status = 'confirmed', updated_at = now()
                    WHERE id = %(id)s AND status = 'pending_payment' AND hold_expires_at > now()
                    """,
                    {"id": booking_id},
                )

            else:  # "failed"
                await cur.execute(
                    """
                    INSERT INTO payments
                        (booking_id, webhook_event_id, amount_cents, status, needs_review, refund_status)
                    VALUES (%(booking_id)s, %(event_id)s, %(amount_cents)s, 'failed', false, 'not_required')
                    """,
                    {"booking_id": booking_id, "event_id": event_id, "amount_cents": amount_cents},
                )
                # No retry/held state in this project's state machine -- a
                # failed charge releases the seat rather than leaving it
                # stuck in pending_payment. A no-op if the booking already
                # moved on (confirmed by an earlier event, or cancelled).
                await cur.execute(
                    """
                    UPDATE bookings
                    SET status = 'cancelled', updated_at = now()
                    WHERE id = %(id)s AND status = 'pending_payment'
                    """,
                    {"id": booking_id},
                )
