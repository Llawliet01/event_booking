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

## Stage 2: proving the naive version double-books (intentional)

`POST /bookings` right now is deliberately unsafe: it does a plain `SELECT` to check the
seat is free, then an `INSERT` — two separate statements, no transaction-level or DB-level
guard between them. Migration `0002` drops `uq_bookings_active_seat`, the one constraint that
was backstopping it, specifically so this bug is reproducible. Stage 3 fixes both.

Run the proof (after `docker compose up --build`, migrations applied, and `python scripts/seed.py`):
```
python scripts/naive_race_test.py
```
This fires 200 concurrent `POST /bookings` requests at the same seat (id 1), then queries the
DB directly for how many "active" bookings ended up on it. Measured output (repeated runs,
local machine, `POOL_MAX=10`):
```
Fired 200 concurrent requests at seat_id=1
Status codes: {201: 10, 409: 190}
Active bookings now sitting on seat_id=1: 10
DOUBLE BOOKING CONFIRMED: 10 active bookings on one seat.
```
10 successes, not 1 — exactly `POOL_MAX`. All 10 pooled connections pick up a request at
nearly the same instant, each runs its own `SELECT` (seeing zero existing bookings), and each
proceeds to `INSERT`. The very first invocation after the pool has just opened can undercount,
since the first few connections are still being established sequentially rather than firing
together; re-running once the pool is warm reproduces it reliably.

## Stage 3: the fix — two interchangeable strategies, same proof script

Migration `0003` restores `uq_bookings_active_seat`. Because Stage 2's own proof run left
genuine duplicate active bookings on seat 1, the migration first resolves those (keeps the
earliest, expires the rest) before re-creating the constraint — the same move you'd make
against real production data when retrofitting a uniqueness guarantee.

`app/bookings/repository.py` now offers two concurrency-safe strategies, chosen by the
`BOOKING_STRATEGY` env var (`A` or `B`, default `A`) so they can be compared later in the k6
load test:
- **A — partial-unique-index insert.** One transaction: expire this seat's stale holds, then
  insert. If a concurrent request already won, our insert violates the (now-restored) unique
  index; we catch that and return 409. `READ COMMITTED` (Postgres's default) is enough —
  correctness comes from the index, not the isolation level.
- **B — `SELECT ... FOR UPDATE`.** Locks the seat row first, so concurrent attempts on that
  seat serialize: only one transaction reaches the insert at a time, and the loser's
  re-check of active bookings sees the winner's already-committed row.

Neither uses an app-level lock, a global lock, or in-memory state — both are safe with
multiple API instances because the guarantee lives entirely in Postgres.

Re-running the *exact same* proof script from Stage 2 (`python scripts/naive_race_test.py`,
200 concurrent requests at seat 1) against each strategy, 3 runs each, local machine,
`POOL_MAX=10`:
```
Status codes: {201: 1, 409: 199}
Active bookings now sitting on seat_id=1: 1
No double booking: exactly 1 active booking on the seat.
```
Identical result for both `BOOKING_STRATEGY=A` and `BOOKING_STRATEGY=B`, every run: exactly
one booking succeeds, 199 get 409, and the DB confirms exactly one active booking on the seat
— a direct before/after contrast with Stage 2's `{201: 10, 409: 190}`.

Later stages will add auth, idempotency, payments, tests, k6 load tests, and the full README
(live demo URL, demo credentials, measured benchmark results).
