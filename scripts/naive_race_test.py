"""Fires N concurrent POST /bookings at the same seat and proves the naive
Stage 2 implementation double-books it.

Requires the API + Postgres running (docker compose up) with migration 0002
applied (drops the unique-index guard) and migration 0003 NOT yet applied.

Usage: python scripts/naive_race_test.py
"""

import asyncio
import os
from collections import Counter

import httpx
import psycopg

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
DATABASE_URL = os.environ["DATABASE_URL"]
CONCURRENCY = int(os.environ.get("RACE_CONCURRENCY", "200"))
SEAT_ID = int(os.environ.get("RACE_SEAT_ID", "1"))
USER_ID = int(os.environ.get("RACE_USER_ID", "1"))


def reset_seat(seat_id: int) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bookings WHERE seat_id = %s", (seat_id,))


def count_active_bookings(seat_id: int) -> int:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM bookings
                WHERE seat_id = %s AND status IN ('held', 'pending_payment', 'confirmed')
                """,
                (seat_id,),
            )
            return cur.fetchone()[0]


async def attempt(client: httpx.AsyncClient) -> int:
    resp = await client.post(
        f"{BASE_URL}/bookings", json={"user_id": USER_ID, "seat_id": SEAT_ID}
    )
    return resp.status_code


async def fire() -> Counter:
    async with httpx.AsyncClient(timeout=30.0) as client:
        results = await asyncio.gather(*(attempt(client) for _ in range(CONCURRENCY)))
    return Counter(results)


def main() -> None:
    reset_seat(SEAT_ID)
    status_counts = asyncio.run(fire())
    active = count_active_bookings(SEAT_ID)

    print(f"Fired {CONCURRENCY} concurrent requests at seat_id={SEAT_ID}")
    print(f"Status codes: {dict(status_counts)}")
    print(f"Active bookings now sitting on seat_id={SEAT_ID}: {active}")

    if active > 1:
        print(f"DOUBLE BOOKING CONFIRMED: {active} active bookings on one seat.")
    else:
        print("No double booking observed this run (race window not hit).")


if __name__ == "__main__":
    main()
