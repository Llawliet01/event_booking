"""Read-only event/seat queries.

list_seat_availability treats an expired hold as free without needing any
cleanup job to have run first: the LEFT JOIN only matches a booking row
that is 'confirmed', or 'held'/'pending_payment' with hold_expires_at still
in the future. A seat whose only booking row has lapsed joins to nothing
and falls through to 'available'.
"""
import datetime as dt
from dataclasses import dataclass

from psycopg import AsyncConnection


@dataclass
class Event:
    id: int
    name: str
    starts_at: dt.datetime


@dataclass
class SeatAvailability:
    id: int
    label: str
    availability: str  # "available" | "held" | "booked"


async def list_events(conn: AsyncConnection) -> list[Event]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id, name, starts_at FROM events ORDER BY starts_at")
        rows = await cur.fetchall()
    return [Event(id=r[0], name=r[1], starts_at=r[2]) for r in rows]


async def event_exists(conn: AsyncConnection, event_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM events WHERE id = %(event_id)s", {"event_id": event_id})
        return await cur.fetchone() is not None


async def list_seat_availability(conn: AsyncConnection, event_id: int) -> list[SeatAvailability]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT s.id, s.label,
                   CASE
                     WHEN b.status = 'confirmed' THEN 'booked'
                     WHEN b.status IN ('held', 'pending_payment') THEN 'held'
                     ELSE 'available'
                   END AS availability
            FROM seats s
            LEFT JOIN bookings b
                ON b.seat_id = s.id
               AND b.status IN ('held', 'pending_payment', 'confirmed')
               AND (b.status = 'confirmed' OR b.hold_expires_at > now())
            WHERE s.event_id = %(event_id)s
            ORDER BY s.label
            """,
            {"event_id": event_id},
        )
        rows = await cur.fetchall()
    return [SeatAvailability(id=r[0], label=r[1], availability=r[2]) for r in rows]
