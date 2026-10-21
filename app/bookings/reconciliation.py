"""Finds "paid but not confirmed" bookings -- the crash-after-payment case.

app/payments/repository.py already flags this synchronously when it
notices a hold lapsed right as a 'succeeded' webhook arrives. This function
is the independent safety net for everything else that could produce the
same inconsistency (a crash between the payment commit and the confirm
step, an application bug, a bad migration): it doesn't assume one specific
point of failure, it just re-derives the invariant "a succeeded payment
implies either a confirmed booking or an explicit needs_review flag" and
fixes any violation it finds.

Not wired to any scheduler -- same TODO(job-queue) pattern as
app/jobs/release_expired_holds.py and app/idempotency/cleanup.py.
"""
from dataclasses import dataclass

from psycopg import AsyncConnection


@dataclass
class ReconciledPayment:
    payment_id: int
    booking_id: int


async def reconcile_payments(conn: AsyncConnection) -> list[ReconciledPayment]:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE payments p
                SET needs_review = true, refund_status = 'pending'
                FROM bookings b
                WHERE p.booking_id = b.id
                  AND p.status = 'succeeded'
                  AND p.needs_review = false
                  AND b.status != 'confirmed'
                RETURNING p.id, p.booking_id
                """
            )
            rows = await cur.fetchall()
    return [ReconciledPayment(payment_id=r[0], booking_id=r[1]) for r in rows]
