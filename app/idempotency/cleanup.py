"""Deletes idempotency_keys rows older than the configured retention.

Not wired to any scheduler -- same TODO(job-queue) pattern as
app/jobs/release_expired_holds.py: plug this into whatever background job
runner gets built later. Nothing in the booking flow depends on this
running; it exists purely so the table doesn't grow without bound.
"""
from psycopg import AsyncConnection


async def cleanup_old_idempotency_keys(conn: AsyncConnection, retention_days: int) -> int:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM idempotency_keys
                WHERE created_at <= now() - (%(days)s * interval '1 day')
                """,
                {"days": retention_days},
            )
            count = cur.rowcount
    return count
