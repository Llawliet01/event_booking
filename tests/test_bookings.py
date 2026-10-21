import httpx

from app.db import get_pool
from tests.conftest import auth_header, make_event_with_seats, make_users


async def test_hold_expiry_frees_seat_and_old_booking_becomes_expired(
    client: httpx.AsyncClient,
) -> None:
    event_id, seat_ids = await make_event_with_seats(n_seats=1)
    seat_id = seat_ids[0]
    user_ids = await make_users(2, prefix="expiry")

    headers1 = auth_header(user_ids[0])
    headers1["Idempotency-Key"] = "expiry-1"
    resp1 = await client.post("/bookings", headers=headers1, json={"seat_id": seat_id})
    assert resp1.status_code == 201
    booking_id = resp1.json()["id"]

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id = %s",
                (booking_id,),
            )
        await conn.commit()

    seats_resp = await client.get(f"/events/{event_id}/seats")
    seat = next(s for s in seats_resp.json() if s["id"] == seat_id)
    assert seat["availability"] == "available"

    headers2 = auth_header(user_ids[1])
    headers2["Idempotency-Key"] = "expiry-2"
    resp2 = await client.post("/bookings", headers=headers2, json={"seat_id": seat_id})
    assert resp2.status_code == 201

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            assert (await cur.fetchone())[0] == "expired"


async def test_invalid_transition_rejected(client: httpx.AsyncClient) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=1)
    [user_id] = await make_users(1, prefix="invalid")
    headers = auth_header(user_id)
    headers["Idempotency-Key"] = "invalid-1"

    resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_ids[0]})
    booking_id = resp.json()["id"]

    cancel1 = await client.post(f"/bookings/{booking_id}/cancel", headers=auth_header(user_id))
    assert cancel1.status_code == 200
    assert cancel1.json()["status"] == "cancelled"

    cancel2 = await client.post(f"/bookings/{booking_id}/cancel", headers=auth_header(user_id))
    assert cancel2.status_code == 409


async def test_cannot_cancel_someone_elses_booking(client: httpx.AsyncClient) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=1)
    user_ids = await make_users(2, prefix="owner")
    headers = auth_header(user_ids[0])
    headers["Idempotency-Key"] = "owner-1"

    resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_ids[0]})
    booking_id = resp.json()["id"]

    other_cancel = await client.post(
        f"/bookings/{booking_id}/cancel", headers=auth_header(user_ids[1])
    )
    assert other_cancel.status_code == 403

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            assert (await cur.fetchone())[0] == "held"
