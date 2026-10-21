"""Plain function for expiring stale holds across every seat.

Nothing in this codebase schedules this -- every hold attempt
(app/bookings/repository.py) and every availability query
(app/events/repository.py) already treats hold_expires_at <= now() as free
on its own, so correctness never depends on this running. It exists purely
to shrink how many expired rows sit around uncommitted-to-cleanup.

TODO(job-queue): call release_expired_holds() on a schedule (e.g. every 60s)
from whichever background job runner gets plugged in later (a separate
project) -- intentionally not Celery/Redis here, per project scope.
"""
from psycopg import AsyncConnection


async def release_expired_holds(conn: AsyncConnection) -> int:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE bookings
                SET status = 'expired', updated_at = now()
                WHERE status IN ('held', 'pending_payment')
                  AND hold_expires_at <= now()
                """
            )
            count = cur.rowcount
    return count
