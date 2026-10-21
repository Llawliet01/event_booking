"""Webhook tests set up the pending_payment state by calling
start_payment() directly rather than hitting POST /bookings/{id}/pay over
HTTP. Reason: httpx's ASGITransport awaits a request's BackgroundTasks to
completion before returning the response (unlike a real deployed server,
where the client gets its response immediately and the background task
runs after) -- so going through /pay here would mean the automatic
simulated webhook has already fired, by the time the test gets control
back, which would race every manually-crafted webhook call below. Calling
the repository function directly gives these tests exact control over
ordering, the same way the live manual verification in Stage 6 did by
hand-crafting events instead of waiting on timing.
"""
import json

import httpx

from app.bookings.repository import start_payment
from app.config import get_settings
from app.db import get_pool
from app.webhooks.security import sign
from tests.conftest import auth_header, make_event_with_seats, make_user


async def _book_and_start_payment(client: httpx.AsyncClient, user_id: int, seat_id: int) -> int:
    headers = auth_header(user_id)
    headers["Idempotency-Key"] = f"webhook-setup-{seat_id}"
    resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_id})
    booking_id = resp.json()["id"]

    pool = get_pool()
    async with pool.connection() as conn:
        await start_payment(conn, booking_id, user_id)
    return booking_id


async def _send_webhook(client: httpx.AsyncClient, payload: dict) -> httpx.Response:
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign(body, get_settings().WEBHOOK_HMAC_SECRET)
    return await client.post(
        "/webhooks/payment",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": sig},
    )


async def test_bad_signature_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/webhooks/payment",
        content=b'{"event_id":"x","booking_id":1,"amount_cents":100,"status":"succeeded"}',
        headers={"Content-Type": "application/json", "X-Signature": "deadbeef"},
    )
    assert resp.status_code == 401


async def test_malformed_payload_422(client: httpx.AsyncClient) -> None:
    resp = await _send_webhook(client, {"event_id": "malformed-1", "status": "succeeded"})
    assert resp.status_code == 422


async def test_unknown_booking_404(client: httpx.AsyncClient) -> None:
    resp = await _send_webhook(
        client,
        {"event_id": "unknown-1", "booking_id": 999_999_999, "amount_cents": 100, "status": "succeeded"},
    )
    assert resp.status_code == 404


async def test_webhook_delivered_twice_is_idempotent(client: httpx.AsyncClient) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=1)
    user_id = await make_user(prefix="webhook1")
    booking_id = await _book_and_start_payment(client, user_id, seat_ids[0])

    payload = {
        "event_id": "dup-test-1",
        "booking_id": booking_id,
        "amount_cents": 5000,
        "status": "succeeded",
    }
    resp1 = await _send_webhook(client, payload)
    resp2 = await _send_webhook(client, payload)
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM payments WHERE booking_id = %s", (booking_id,))
            assert (await cur.fetchone())[0] == 1
            await cur.execute("SELECT count(*) FROM webhook_events WHERE event_id = %s", ("dup-test-1",))
            assert (await cur.fetchone())[0] == 1
            await cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            assert (await cur.fetchone())[0] == "confirmed"


async def test_hold_expired_before_payment_succeeds_not_confirmed(client: httpx.AsyncClient) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=1)
    user_id = await make_user(prefix="webhook2")
    booking_id = await _book_and_start_payment(client, user_id, seat_ids[0])

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id = %s",
                (booking_id,),
            )
        await conn.commit()

    payload = {
        "event_id": "expiry-webhook-1",
        "booking_id": booking_id,
        "amount_cents": 5000,
        "status": "succeeded",
    }
    resp = await _send_webhook(client, payload)
    assert resp.status_code == 200

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            assert (await cur.fetchone())[0] == "pending_payment"
            await cur.execute(
                "SELECT needs_review, refund_status FROM payments WHERE booking_id = %s",
                (booking_id,),
            )
            assert (await cur.fetchone()) == (True, "pending")


async def test_failed_then_out_of_order_succeeded_does_not_unconfirm(
    client: httpx.AsyncClient,
) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=1)
    user_id = await make_user(prefix="webhook3")
    booking_id = await _book_and_start_payment(client, user_id, seat_ids[0])

    failed = {
        "event_id": "failed-1",
        "booking_id": booking_id,
        "amount_cents": 5000,
        "status": "failed",
    }
    await _send_webhook(client, failed)

    late_succeeded = {
        "event_id": "late-succeeded-1",
        "booking_id": booking_id,
        "amount_cents": 5000,
        "status": "succeeded",
    }
    await _send_webhook(client, late_succeeded)

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            assert (await cur.fetchone())[0] == "cancelled"
