"""Fault injection: prove the transactional guarantee the whole project
leans on, rather than just trusting it.
"""
import os

import psycopg
import pytest

from app.bookings import repository as bookings_repo
from app.db import get_pool
from tests.conftest import make_event_with_seats, make_user


async def test_exception_mid_transaction_rolls_back_everything(monkeypatch) -> None:
    """A stale (already-lapsed) held booking sits on the seat.
    strategy_a_create_hold's first statement expires it; the second
    statement (the insert) is made to raise. If the transaction's
    atomicity holds, that first UPDATE must roll back too, leaving the
    stale booking exactly as it was -- not half-expired, not inserted.
    """
    _, seat_ids = await make_event_with_seats(n_seats=1)
    seat_id = seat_ids[0]
    user_id = await make_user(prefix="fault")

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO bookings (user_id, seat_id, status, hold_expires_at)
                VALUES (%s, %s, 'held', now() - interval '1 minute')
                RETURNING id
                """,
                (user_id, seat_id),
            )
            stale_booking_id = (await cur.fetchone())[0]
        await conn.commit()

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-transaction")

        monkeypatch.setattr(bookings_repo, "_insert_hold", boom)

        with pytest.raises(RuntimeError):
            await bookings_repo.strategy_a_create_hold(conn, user_id, seat_id, 5)

        async with conn.cursor() as cur:
            await cur.execute("SELECT status FROM bookings WHERE id = %s", (stale_booking_id,))
            # The expire-UPDATE ran earlier in the SAME transaction as the
            # failed insert -- it must have rolled back too.
            assert (await cur.fetchone())[0] == "held"

            await cur.execute("SELECT count(*) FROM bookings WHERE seat_id = %s", (seat_id,))
            assert (await cur.fetchone())[0] == 1  # no new row was inserted


async def test_dropped_connection_mid_transaction_persists_nothing() -> None:
    """Deterministic proxy for 'kill the API process mid-request': an
    uncommitted transaction on a connection that disconnects is rolled
    back by Postgres itself, so nothing it wrote ever becomes visible --
    the same guarantee that makes a real process kill mid-request safe.
    Precisely timing an actual `kill -9` mid-transaction isn't
    deterministically reproducible in an automated test, so this exercises
    the underlying mechanism directly instead.
    """
    _, seat_ids = await make_event_with_seats(n_seats=1)
    seat_id = seat_ids[0]
    user_id = await make_user(prefix="crash")

    conn = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"])
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO bookings (user_id, seat_id, status, hold_expires_at) "
            "VALUES (%s, %s, 'held', now() + interval '5 minutes')",
            (user_id, seat_id),
        )
    # No commit -- simulate the process dying right here, mid-transaction.
    await conn.close()

    pool = get_pool()
    async with pool.connection() as fresh_conn:
        async with fresh_conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM bookings WHERE seat_id = %s", (seat_id,))
            assert (await cur.fetchone())[0] == 0
