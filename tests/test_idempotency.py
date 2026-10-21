import asyncio

import httpx

from app.db import get_pool
from app.idempotency.repository import Outcome, begin_idempotent_request, compute_request_hash
from tests.conftest import auth_header, make_event_with_seats, make_user


async def test_same_key_sent_twice_concurrently_one_booking_identical_responses(
    client: httpx.AsyncClient,
) -> None:
    """Two outcomes are both legitimate for the pair of concurrent calls:
    either both land after the claimed one completes (replay -> 201+200,
    or even 200+200), or the losing call's own claim attempt lands while
    the winner is still mid-flight, genuinely in progress (409). In-process
    asyncio.gather races the two coroutines even tighter than two real
    HTTP clients over a real socket (see README Stage 5's {201: 1, 200: 4}
    over real HTTP vs. the 409 this reproduces reliably in-process), so a
    live 409 shows up here more often than it did there. A client that
    gets that 409 is expected to retry -- which is exactly what this test
    does -- and the contract is that retry converges on the same booking,
    not that concurrent calls never observe the in-progress window.
    """
    _, seat_ids = await make_event_with_seats(n_seats=1)
    seat_id = seat_ids[0]
    user_id = await make_user(prefix="idem")

    headers = auth_header(user_id)
    headers["Idempotency-Key"] = "same-key-concurrent"

    async def attempt() -> tuple[int, dict]:
        resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_id})
        return resp.status_code, resp.json()

    async def attempt_with_retry() -> tuple[int, dict]:
        for _ in range(10):
            status, body = await attempt()
            if status != 409 or "seat" in body.get("detail", ""):
                return status, body
            await asyncio.sleep(0.05)
        return status, body

    (status1, body1), (status2, body2) = await asyncio.gather(
        attempt_with_retry(), attempt_with_retry()
    )

    assert status1 in (200, 201)
    assert status2 in (200, 201)
    assert body1["id"] == body2["id"]

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM bookings WHERE seat_id = %s", (seat_id,))
            assert (await cur.fetchone())[0] == 1


async def test_same_key_different_body_is_422(client: httpx.AsyncClient) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=2)
    user_id = await make_user(prefix="idem2")
    headers = auth_header(user_id)
    headers["Idempotency-Key"] = "mismatch-key"

    resp1 = await client.post("/bookings", headers=headers, json={"seat_id": seat_ids[0]})
    assert resp1.status_code == 201

    resp2 = await client.post("/bookings", headers=headers, json={"seat_id": seat_ids[1]})
    assert resp2.status_code == 422

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM bookings WHERE seat_id = %s", (seat_ids[1],))
            assert (await cur.fetchone())[0] == 0


async def test_in_progress_conflict_and_stale_reclaim() -> None:
    """Repository-level, not HTTP: this needs precise control over *when*
    a key gets marked completed, which a fast HTTP round trip doesn't
    leave room for (see README Stage 5 notes on {201: 1, 200: 4}).
    """
    pool = get_pool()
    user_id = await make_user(prefix="idem3")
    request_hash = compute_request_hash({"seat_id": 1})

    async with pool.connection() as conn_a, pool.connection() as conn_b:
        result_a = await begin_idempotent_request(conn_a, user_id, "k", request_hash, 30)
        assert result_a.outcome is Outcome.CLAIMED

        result_b = await begin_idempotent_request(conn_b, user_id, "k", request_hash, 30)
        assert result_b.outcome is Outcome.IN_PROGRESS

        async with conn_a.cursor() as cur:
            await cur.execute(
                "UPDATE idempotency_keys SET updated_at = now() - interval '1 hour' "
                "WHERE user_id = %s AND key = %s",
                (user_id, "k"),
            )
        await conn_a.commit()

        new_hash = compute_request_hash({"seat_id": 2})
        result_c = await begin_idempotent_request(conn_b, user_id, "k", new_hash, 30)
        assert result_c.outcome is Outcome.CLAIMED
