"""The core correctness proof, automated: fires genuinely concurrent
requests (asyncio.gather against the app in-process) and checks the
database directly, not just HTTP status codes.
"""
import asyncio

import httpx
import pytest

from app.db import get_pool
from tests.conftest import auth_header, make_event_with_seats, make_users


@pytest.mark.parametrize("strategy", ["A", "B"])
async def test_200_concurrent_requests_one_seat_exactly_one_wins(
    client: httpx.AsyncClient, monkeypatch, strategy: str
) -> None:
    monkeypatch.setenv("BOOKING_STRATEGY", strategy)
    from app.config import get_settings

    get_settings.cache_clear()

    _, seat_ids = await make_event_with_seats(n_seats=1)
    seat_id = seat_ids[0]
    user_ids = await make_users(200, prefix=f"race{strategy}")

    async def attempt(i: int) -> int:
        headers = auth_header(user_ids[i])
        headers["Idempotency-Key"] = f"race-{strategy}-{i}"
        resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_id})
        return resp.status_code

    results = await asyncio.gather(*(attempt(i) for i in range(200)))

    assert results.count(201) == 1
    assert results.count(409) == 199

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT count(*) FROM bookings
                WHERE seat_id = %s AND status IN ('held', 'pending_payment', 'confirmed')
                """,
                (seat_id,),
            )
            assert (await cur.fetchone())[0] == 1


async def test_500_users_100_seats_no_seat_double_booked(client: httpx.AsyncClient) -> None:
    _, seat_ids = await make_event_with_seats(n_seats=100)
    user_ids = await make_users(500, prefix="spread")

    async def attempt(i: int) -> int:
        seat_id = seat_ids[i % 100]
        headers = auth_header(user_ids[i])
        headers["Idempotency-Key"] = f"spread-{i}"
        resp = await client.post("/bookings", headers=headers, json={"seat_id": seat_id})
        return resp.status_code

    results = await asyncio.gather(*(attempt(i) for i in range(500)))

    assert results.count(201) == 100
    assert results.count(409) == 400

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT seat_id, count(*) FROM bookings
                WHERE status IN ('held', 'pending_payment', 'confirmed')
                GROUP BY seat_id
                HAVING count(*) > 1
                """
            )
            assert await cur.fetchall() == []
