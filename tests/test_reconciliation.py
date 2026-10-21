"""Reconciliation's job is the crash variant of the hold-expiry case
already covered in test_webhooks.py: a payment that succeeded, recorded,
but was never flagged or confirmed, with no webhook delivery involved at
all -- constructed directly against the DB to simulate a crash between
the payment commit and the confirm step.
"""
import httpx

from app.bookings.reconciliation import reconcile_payments
from app.bookings.repository import start_payment
from app.db import get_pool
from tests.conftest import auth_header, make_event_with_seats, make_user


async def test_reconciliation_finds_and_fixes_unflagged_succeeded_payment(
    client: httpx.AsyncClient,
) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=1)
    user_id = await make_user(prefix="recon")

    headers = auth_header(user_id)
    headers["Idempotency-Key"] = "recon-setup-1"
    resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_ids[0]})
    booking_id = resp.json()["id"]

    pool = get_pool()
    async with pool.connection() as conn:
        await start_payment(conn, booking_id, user_id)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO webhook_events (event_id, payload) VALUES ('crash-sim-1', '{}')"
            )
            await cur.execute(
                """
                INSERT INTO payments
                    (booking_id, webhook_event_id, amount_cents, status, needs_review, refund_status)
                VALUES (%s, 'crash-sim-1', 5000, 'succeeded', false, 'not_required')
                RETURNING id
                """,
                (booking_id,),
            )
            payment_id = (await cur.fetchone())[0]
        await conn.commit()

    async with pool.connection() as conn:
        reconciled = await reconcile_payments(conn)
    assert [r.payment_id for r in reconciled] == [payment_id]

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT needs_review, refund_status FROM payments WHERE id = %s", (payment_id,)
            )
            assert (await cur.fetchone()) == (True, "pending")

    # Idempotent: nothing left to fix on a second pass.
    async with pool.connection() as conn:
        second_pass = await reconcile_payments(conn)
    assert second_pass == []
