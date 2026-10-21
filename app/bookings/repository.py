"""Concurrency-safe seat-hold logic.

Two interchangeable strategies, selected via Settings.BOOKING_STRATEGY so
they can be compared directly in the load test:

  A) partial-unique-index insert -- optimistic: just attempt the insert and
     let the DB's partial unique index (uq_bookings_active_seat) reject a
     second active booking on the same seat with a unique-violation.
  B) SELECT ... FOR UPDATE -- pessimistic: lock the seat row first, so only
     one transaction at a time can even reach the insert for that seat.

Neither strategy uses an app-level lock or in-memory state, so both are safe
across any number of API instances. Stage 2's naive version (a plain
SELECT-then-INSERT with nothing synchronizing them) lived in this file first
and is preserved in git history; this replaces it.

cancel_booking and list_bookings_for_user support the owner-only cancel and
GET /bookings/me endpoints; cancel_booking defers to
app.bookings.state_machine.can_transition for which status changes are
legal instead of re-deriving the rule here.
"""
from dataclasses import dataclass

from psycopg import AsyncConnection, errors

from app.bookings.state_machine import can_transition


class SeatTakenError(Exception):
    pass


class SeatNotFoundError(Exception):
    pass


class BookingNotFoundError(Exception):
    pass


class NotOwnerError(Exception):
    pass


class InvalidTransitionError(Exception):
    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"cannot transition booking from {from_status!r} to {to_status!r}")


@dataclass
class Booking:
    id: int
    seat_id: int
    user_id: int
    status: str


async def _expire_stale_holds(cur, seat_id: int) -> None:
    await cur.execute(
        """
        UPDATE bookings
        SET status = 'expired', updated_at = now()
        WHERE seat_id = %(seat_id)s
          AND status IN ('held', 'pending_payment')
          AND hold_expires_at <= now()
        """,
        {"seat_id": seat_id},
    )


async def _insert_hold(cur, user_id: int, seat_id: int, hold_minutes: int) -> Booking:
    await cur.execute(
        """
        INSERT INTO bookings (user_id, seat_id, status, hold_expires_at)
        VALUES (%(user_id)s, %(seat_id)s, 'held', now() + (%(minutes)s * interval '1 minute'))
        RETURNING id, seat_id, user_id, status
        """,
        {"user_id": user_id, "seat_id": seat_id, "minutes": hold_minutes},
    )
    row = await cur.fetchone()
    return Booking(id=row[0], seat_id=row[1], user_id=row[2], status=row[3])


async def strategy_a_create_hold(
    conn: AsyncConnection, user_id: int, seat_id: int, hold_minutes: int
) -> Booking:
    """One transaction: expire this seat's stale holds, then insert. A
    concurrent winner's commit makes our insert violate
    uq_bookings_active_seat -- we catch that and report 409. READ COMMITTED
    (Postgres's default) is enough: correctness comes from the unique index,
    not from the isolation level.
    """
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await _expire_stale_holds(cur, seat_id)
                booking = await _insert_hold(cur, user_id, seat_id, hold_minutes)
    except errors.UniqueViolation:
        raise SeatTakenError()
    return booking


async def strategy_b_create_hold(
    conn: AsyncConnection, user_id: int, seat_id: int, hold_minutes: int
) -> Booking:
    """Locking the seat row serializes every concurrent attempt on it: only
    one transaction reaches the insert at a time, so the second one's
    re-check of active bookings sees the first one's already-committed row
    and reports 409 before ever attempting an insert.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM seats WHERE id = %(seat_id)s FOR UPDATE",
                {"seat_id": seat_id},
            )
            if await cur.fetchone() is None:
                raise SeatNotFoundError()

            await _expire_stale_holds(cur, seat_id)

            await cur.execute(
                """
                SELECT 1 FROM bookings
                WHERE seat_id = %(seat_id)s
                  AND status IN ('held', 'pending_payment', 'confirmed')
                """,
                {"seat_id": seat_id},
            )
            if await cur.fetchone() is not None:
                raise SeatTakenError()

            booking = await _insert_hold(cur, user_id, seat_id, hold_minutes)
    return booking


async def create_hold(
    conn: AsyncConnection,
    user_id: int,
    seat_id: int,
    hold_minutes: int,
    strategy: str,
) -> Booking:
    if strategy == "A":
        return await strategy_a_create_hold(conn, user_id, seat_id, hold_minutes)
    if strategy == "B":
        return await strategy_b_create_hold(conn, user_id, seat_id, hold_minutes)
    raise ValueError(f"unknown booking strategy: {strategy!r}")


async def cancel_booking(conn: AsyncConnection, booking_id: int, user_id: int) -> Booking:
    """Owner-only cancel, gated by the state machine. Locks the booking row
    first (FOR UPDATE) so a concurrent operation on the same booking (e.g. a
    payment webhook confirming it) can't race this cancel.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, user_id, status FROM bookings WHERE id = %(id)s FOR UPDATE",
                {"id": booking_id},
            )
            row = await cur.fetchone()
            if row is None:
                raise BookingNotFoundError()

            _, owner_id, status = row
            if owner_id != user_id:
                raise NotOwnerError()
            if not can_transition(status, "cancelled"):
                raise InvalidTransitionError(status, "cancelled")

            await cur.execute(
                """
                UPDATE bookings
                SET status = 'cancelled', updated_at = now()
                WHERE id = %(id)s
                RETURNING id, seat_id, user_id, status
                """,
                {"id": booking_id},
            )
            updated = await cur.fetchone()
    return Booking(id=updated[0], seat_id=updated[1], user_id=updated[2], status=updated[3])


async def list_bookings_for_user(conn: AsyncConnection, user_id: int) -> list[Booking]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, seat_id, user_id, status
            FROM bookings
            WHERE user_id = %(user_id)s
            ORDER BY created_at DESC
            """,
            {"user_id": user_id},
        )
        rows = await cur.fetchall()
    return [Booking(id=r[0], seat_id=r[1], user_id=r[2], status=r[3]) for r in rows]
