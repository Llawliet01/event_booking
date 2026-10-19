# Event Booking Backend

Concurrency-safe seat booking API. Full design rationale lives in [DESIGN.md](DESIGN.md) (added from Stage 6 onward).

## Local development (Stage 1: schema + health only, no booking logic yet)

1. Copy env file:
   ```
   cp .env.example .env
   ```
2. Start Postgres + API:
   ```
   docker compose up --build
   ```
3. Run migrations (from the host, against the compose Postgres on localhost:5432,
   or `docker compose exec api alembic upgrade head` once the API container is up):
   ```
   alembic upgrade head
   ```
4. Seed demo data:
   ```
   python scripts/seed.py
   ```
5. Check health:
   ```
   curl http://localhost:8000/health
   curl http://localhost:8000/health/ready
   ```

**Running the API directly on a Windows host** (skipping the `api` container, e.g. for faster
iteration): use `python run.py`, not the `uvicorn` CLI directly. psycopg3's async mode is
incompatible with Windows' default `ProactorEventLoop`, and the `uvicorn` CLI creates its event
loop before it ever imports application code, so the fix has to live in a launcher script that
runs before uvicorn starts. This doesn't affect Docker/Render, which run uvicorn's CLI on Linux.

Later stages will add booking endpoints, auth, idempotency, payments, tests, k6 load tests,
and the full README (live demo URL, demo credentials, measured benchmark results).
