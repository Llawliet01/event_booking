from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    settings = get_settings()
    _pool = AsyncConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=settings.POOL_MIN,
        max_size=settings.POOL_MAX,
        # The DB may sit behind PgBouncer in transaction-pooling mode, which
        # breaks server-side prepared statements tied to a session. Disabling
        # psycopg3's prepared-statement cache keeps every query a plain
        # extended-protocol query, safe under transaction pooling.
        kwargs={"autocommit": False, "prepare_threshold": None},
        open=False,
    )
    await _pool.open(wait=True)


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Connection pool is not open yet")
    return _pool
