"""Shared fixtures for the real-Postgres test suite.

These tests run the FastAPI app in-process via httpx's ASGITransport
(no Docker/uvicorn needed for the API itself) against a real Postgres --
only the database needs to actually be running (`docker compose up -d
postgres`), matching the project's "real Postgres, not mocks" rule.
"""
import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ.setdefault("DATABASE_URL", "postgresql://booking:booking@localhost:5432/booking")
os.environ.setdefault("JWT_SECRET", "test-secret-key")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("WEBHOOK_HMAC_SECRET", "test-webhook-secret")
os.environ.setdefault("CORS_ALLOWED_ORIGIN", "http://localhost:3000")
os.environ.setdefault("PAYMENT_SIMULATOR_DELAY_SECONDS", "1.0")

import httpx
import pytest
import pytest_asyncio

from app.auth.security import create_access_token
from app.config import get_settings
from app.db import close_pool, get_pool, open_pool
from app.main import app


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _db_pool():
    await open_pool()
    yield
    await close_pool()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    # Several tests monkeypatch env vars (e.g. BOOKING_STRATEGY) and expect
    # get_settings() to pick them up; without clearing the lru_cache both
    # before and after, a stale Settings instance could leak between tests
    # in either direction.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_db(_db_pool):
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "TRUNCATE payments, webhook_events, idempotency_keys, bookings, "
                "seats, events, users RESTART IDENTITY CASCADE"
            )
        await conn.commit()
    yield


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def make_users(n: int, prefix: str = "user") -> list[int]:
    """Bulk-inserts n users directly (dummy password hash -- these tests
    mint JWTs via create_access_token() and never go through /auth/login,
    so hashing speed would otherwise dominate setup time for nothing).
    """
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s)",
                [(f"{prefix}{i}@example.com", "x") for i in range(n)],
            )
        await conn.commit()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM users WHERE email LIKE %s ORDER BY id",
                (f"{prefix}%@example.com",),
            )
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def make_user(prefix: str = "user") -> int:
    (user_id,) = await make_users(1, prefix=prefix)
    return user_id


async def make_event_with_seats(n_seats: int = 1) -> tuple[int, list[int]]:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO events (name, starts_at) "
                "VALUES ('Test Event', now() + interval '1 day') RETURNING id"
            )
            event_id = (await cur.fetchone())[0]
            await cur.executemany(
                "INSERT INTO seats (event_id, label) VALUES (%s, %s)",
                [(event_id, f"S{i}") for i in range(n_seats)],
            )
        await conn.commit()
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM seats WHERE event_id = %s ORDER BY id", (event_id,))
            rows = await cur.fetchall()
    return event_id, [r[0] for r in rows]


def auth_header(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}
