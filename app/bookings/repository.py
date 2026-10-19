"""Naive (intentionally unsafe) seat-hold logic — the Stage 2 race demo.

This checks seat availability and inserts the hold as two separate
statements with no transaction-level or DB-level protection between them:
two concurrent requests can both see the seat as free and both insert.
Migration 0002 drops the partial unique index precisely so nothing backs
this up at the DB layer either. Stage 3 replaces this function's body with
a concurrency-safe strategy and migration 0003 restores the index.
"""
from dataclasses import dataclass

from psycopg import AsyncConnection


class SeatTakenError(Exception):
    pass


@dataclass
class Booking:
    id: int
    seat_id: int
    user_id: int
    status: str


async def naive_create_hold(
    conn: AsyncConnection, user_id: int, seat_id: int, hold_minutes: int
) -> Booking:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1 FROM bookings
            WHERE seat_id = %(seat_id)s
              AND (
                status = 'confirmed'
                OR (status IN ('held', 'pending_payment') AND hold_expires_at > now())
              )
            """,
            {"seat_id": seat_id},
        )
        if await cur.fetchone() is not None:
            raise SeatTakenError()

        await cur.execute(
            """
            INSERT INTO bookings (user_id, seat_id, status, hold_expires_at)
            VALUES (%(user_id)s, %(seat_id)s, 'held', now() + (%(minutes)s * interval '1 minute'))
            RETURNING id, seat_id, user_id, status
            """,
            {"user_id": user_id, "seat_id": seat_id, "minutes": hold_minutes},
        )
        row = await cur.fetchone()

    await conn.commit()
    return Booking(id=row[0], seat_id=row[1], user_id=row[2], status=row[3])
