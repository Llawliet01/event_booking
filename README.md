# Event Booking Backend

Concurrency-safe seat booking API. Full design rationale lives in [DESIGN.md](DESIGN.md).

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

## Stage 4: auth, hold expiry, the state machine, cancel

- **Auth.** `POST /auth/register` and `POST /auth/login` (argon2 password hashing, JWT access
  tokens). `POST /bookings`, `POST /bookings/{id}/cancel`, and `GET /bookings/me` now require
  `Authorization: Bearer <token>`; the caller's identity comes from the JWT, not the request
  body. Duplicate email registration returns 409; bad credentials return 401; a missing/invalid
  token returns 403/401 respectively (FastAPI's `HTTPBearer` default vs. our own check).
- **Seat availability.** `GET /events` and `GET /events/{id}/seats` — the latter's query joins
  to a booking row only if it's `confirmed`, or `held`/`pending_payment` with `hold_expires_at`
  still in the future, so a lapsed hold reads as `available` with zero cleanup required.
- **State machine.** `app/bookings/state_machine.py` is the single function (`can_transition`)
  everything else defers to for which status changes are legal; 27 unit tests
  (`tests/test_state_machine.py`) exhaustively cover all 25 `(from, to)` status pairs.
- **Cancel.** `POST /bookings/{id}/cancel` — owner-only (403 otherwise), 404 if the booking
  doesn't exist, 409 if the state machine rejects the transition (e.g. cancelling an
  already-cancelled booking).
- **Job hook.** `app/jobs/release_expired_holds.py` — a plain function, not wired to any
  scheduler, for a background job to call later. Nothing in the app depends on it running:
  every hold attempt and every availability query already treats `hold_expires_at <= now()`
  as free on its own.

All verified against a live container, not just unit tests: registered two real users,
confirmed 409/401/403/404 on every documented error path, booked a seat and watched it flip
from `available` to `held` in the seats listing, force-expired a hold directly in Postgres and
confirmed both the availability query and a second user's booking attempt treated it as free
(the original row flips to `expired` as a side effect of the second attempt's own hold-insert,
with no cron involved), and ran `release_expired_holds()` directly to confirm it independently
expires lapsed holds.

## Stage 5: idempotency keys

`POST /bookings` now requires an `Idempotency-Key` header (missing it is a 422 from FastAPI's
own header validation, before the handler ever runs). `app/idempotency/repository.py` claims
the key in its own committed transaction *before* attempting the hold, so a crash partway
through leaves the key `in_progress` for the stale-reclaim path to pick up later rather than
silently losing track of the attempt:

1. `INSERT ... ON CONFLICT (user_id, key) DO NOTHING` — claims the key if it's unseen.
2. Already exists and `completed` with the same request hash → **replay**: return the stored
   response, but always as `200`, even if the original result was `201` or a `409`/`404`
   business rejection — `200` here means "here's the cached result of a request that already
   ran," not "this succeeded."
3. Already exists and `completed` with a *different* hash → `422`, and the existing completed
   record is left untouched.
4. Already exists and `in_progress`, and still fresh (`updated_at` within
   `IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS`, default 30s) → `409` — another request with this
   exact key is genuinely still running.
5. Already exists and `in_progress`, but stale (older than the timeout — the original attempt
   most likely crashed before marking the key `completed`) → reclaim it via a CAS `UPDATE`
   that only succeeds if it's still `in_progress` and still stale at that exact moment; a lost
   race just loops back to re-resolve against the current row.

`app/idempotency/cleanup.py` has `cleanup_old_idempotency_keys()` — same documented
not-wired-to-a-scheduler pattern as `release_expired_holds()`.

Verified directly (both over HTTP and by calling `begin_idempotent_request`/
`complete_idempotent_request` on separate connections, since steps 2-5 above need precise
control over *when* a key gets marked completed — something a fast-completing HTTP round trip
doesn't leave much room for):
- Missing header → `422`. Fresh create → `201`.
- Same key + same body, replayed → `200` with the identical stored booking.
- Same key + different body → `422`; the seat the mismatched body named was never touched.
- Two connections racing the same unclaimed key, neither completing it: first → `CLAIMED`,
  second → `IN_PROGRESS` (the live 409 path). A third call after the first completes → `REPLAY`.
- A claimed-but-never-completed key, immediately retried → `IN_PROGRESS` (correctly still
  fresh); after forcing its `updated_at` back by an hour (simulating a crashed attempt, no
  app-code changes involved) → the same key, now with a *different* body, is successfully
  reclaimed (`CLAIMED`), and the stored `request_hash` updates to match the new attempt.
- `cleanup_old_idempotency_keys(retention_days=7)` against 4 rows aged to 10 days old deleted
  all 4.
- Five genuinely concurrent HTTP requests with the same fresh key against the real endpoint
  came back `{201: 1, 200: 4}` with zero 409s — the hold itself is fast enough to complete
  before the other requests' claims resolve, so they land on `REPLAY` rather than
  `IN_PROGRESS`. Both are correct outcomes of the same mechanism; which one you see depends on
  how long the claimed request takes to finish.

## Stage 6: payment simulation, signed idempotent webhook, reconciliation

- **`POST /bookings/{id}/pay`** — `held -> pending_payment`, marking the booking `expired`
  instead if its hold had already lapsed (409). On success it schedules
  `app/payments/provider_simulator.py` as a FastAPI background task: after a short delay it
  HMAC-signs a fake event and does a real HTTP POST to our own `/webhooks/payment`, the same
  path a real provider's callback would take from the outside.
- **`POST /webhooks/payment`** — verifies the signature over the raw request body (not a
  re-serialized model, which could reorder keys and break the signature), dedupes by
  `event_id` (its own primary key), then records a `payments` row and conditionally confirms
  the booking — dedupe, record, and confirm all in one transaction
  (`app/payments/repository.py`), so a crash anywhere rolls back everything including the
  dedupe row, and a provider retry replays cleanly.
- **`app/bookings/reconciliation.py`** — `reconcile_payments()` re-derives the invariant "a
  succeeded payment implies a confirmed booking or an explicit `needs_review` flag" and fixes
  any violation, independent of how it happened (crash, bug, bad migration). Same
  not-wired-to-a-scheduler pattern as the other job hooks.

Verified live end to end, including the cases that don't show up from the happy path alone:
- Full flow: book → pay (`pending_payment`) → simulated signed webhook lands ~1s later →
  `confirmed`, with a correct `payments` row (`status=succeeded, needs_review=false`).
- Bad signature → `401`. Malformed payload → `422`. Unknown `booking_id` → `404`.
- The exact same webhook event posted twice → `webhook_events` and `payments` both show
  exactly one row for it — the second delivery is a true no-op, not just a 200 that hides a
  duplicate write.
- **Hold expired before the payment succeeded**: forced a hold into the past, then sent a
  validly-signed `succeeded` webhook for it directly (bypassing the simulator's timing, to
  remove any race with the background task) — the booking stayed `pending_payment` (never
  confirmed) and the payment landed with `needs_review=true, refund_status='pending'`.
- **Failed payment**: a `failed` webhook event cancelled the booking (freeing the seat); a
  *different*, later `succeeded` event for the same booking (the automatic simulator's own
  callback, arriving after) did not un-cancel it — the confirm step only ever fires from
  `pending_payment`, which this booking had already left.
- **Reconciliation**: manually constructed the "crash between payment and confirm" state
  directly in Postgres (bypassing the webhook handler entirely — a `pending_payment` booking
  with an unflagged `succeeded` payment row), ran `reconcile_payments()`, and confirmed it
  found and flagged exactly that row (`needs_review` false → true, `refund_status` ·
  `not_required` → `pending`); a second pass found nothing left to fix.

One timing lesson from this stage worth keeping: an early version of the hold-expiry test gave
a false pass, because the several sequential `docker exec`/`curl` calls in a shell script ate
more wall-clock time than the 1-second simulated payment delay, so the *automatic* webhook
callback landed and confirmed the booking before the test's own "force the hold into the past"
step ever ran. Rewriting the test as a single async Python script with precise control over
ordering (and bypassing the automatic callback by sending a hand-crafted webhook instead)
removed the ambiguity. The lesson generalizes: a test that measures something time-sensitive
needs to control time, not just hope its own overhead stays small enough.

## Stage 7: tests, CI, observability

**Tests** (`tests/`, 45 tests, run against a real Postgres -- no mocks): the app runs
in-process via `httpx.ASGITransport` (no Docker/uvicorn needed for the API itself, only
`docker compose up -d postgres`), so `pytest tests/` is enough locally.

- `test_concurrency.py` — the Stage 2/3 proof, automated and parametrized over both booking
  strategies: 200 concurrent requests on one seat (exactly 1 success, 199 conflicts, checked
  against the DB directly, not just HTTP codes) and 500 users across 100 seats (exactly 100
  successes, zero seats with more than one active booking).
- `test_idempotency.py` — same key twice concurrently (converges on one booking via a
  realistic client retry on the 409-in-progress window — see note below); same key, different
  body → 422; the in-progress/stale-reclaim mechanism driven directly at the repository level
  for precise timing control.
- `test_bookings.py` — hold expiry frees the seat and flips the old booking to `expired`;
  double-cancel is rejected (409); cancelling someone else's booking is rejected (403).
- `test_webhooks.py` / `test_reconciliation.py` — bad signature (401), malformed payload (422),
  unknown booking (404), the same event delivered twice (exactly one row, not just one 200), a
  hold that lapsed before a `succeeded` webhook arrived (not confirmed, flagged for review), a
  `failed` event followed by an out-of-order `succeeded` one for the same booking (stays
  cancelled), and `reconcile_payments()` finding and fixing a manually-crafted inconsistent row.
- `test_fault_injection.py` — an exception raised mid-transaction (after an earlier statement
  in the *same* transaction already ran) rolls back everything, proven by checking that earlier
  statement's effect was undone too, not just that the final insert didn't happen; and a
  connection dropped mid-transaction, uncommitted, leaves nothing visible from a fresh
  connection afterward — the deterministic mechanism behind "kill the process mid-request is
  safe," since precisely timing an actual `kill -9` mid-transaction isn't reproducible in an
  automated test.

**A real bug the test suite caught that manual testing didn't**: `test_malformed_payload_422`
failed on its first run with a `401` instead. The test file hardcoded the webhook secret string
used by `conftest.py`'s default, but the shell session running pytest had `.env` exported into
its environment first, which `os.environ.setdefault()` correctly left in place — meaning the
*app* was signing/verifying against the real `.env` secret while the *test* was still signing
against the hardcoded one. Fixed by having the test read `get_settings().WEBHOOK_HMAC_SECRET` at
call time instead of assuming a literal value. Re-ran the whole suite with a fully clean
environment (`env -i`, only the exact variables the CI workflow sets) to confirm this wasn't
papering over a deeper problem — all 45 pass either way.

**A genuine race the concurrency rewrite surfaced**: the first version of the "same key sent
twice concurrently" idempotency test assumed the pair would always resolve to a replay (as the
live HTTP test in Stage 5 observed: `{201: 1, 200: 4}`, zero 409s). It didn't — in-process
`asyncio.gather` races the two coroutines tighter than two real HTTP clients over a real socket
ever did, and the losing call's own claim attempt reliably landed while the winner was still
mid-flight, genuinely in progress (409), not yet replayable. The fix wasn't to loosen the test;
it was to make it behave like a real client would: retry on that specific 409 and assert the
retry converges on the same booking. The contract was never "concurrent calls never observe the
in-progress window" — it was "the system behaves correctly when they do."

**CI** (`.github/workflows/ci.yml`): runs on every push and PR, against a `postgres:16` service
container, applying migrations then running the full suite — verified locally by running the
exact same steps (`alembic upgrade head`, `pytest tests/ -v`) against a completely clean
environment variable set (`env -i`, nothing but what the workflow itself exports) to confirm
there's no hidden dependency on the local `.env`.

**Observability**: `app/observability/middleware.py` assigns/propagates an `X-Request-ID` on
every request and logs one structured JSON line per request (method, path, status, duration —
never headers or bodies, so tokens and passwords never reach the logs) via `structlog`.
`GET /metrics` (Prometheus text format) exposes `bookings_success_total`,
`bookings_conflict_total`, an `http_request_duration_seconds` histogram, and
`db_pool_connections_total`/`db_pool_connections_in_use` gauges read from the live connection
pool at scrape time. Verified live: booked a seat, confirmed `bookings_success_total` ticked up
by exactly 1; confirmed the pool gauges matched reality.

**Rate limiting**: `app/auth/rate_limit.py` is a fixed-window limiter (`AUTH_RATE_LIMIT_PER_MINUTE`,
default 5/min) on `/auth/register` and `/auth/login`, deliberately per-instance and in-memory —
this project has no Redis, so a distributed limiter is out of scope; that's an accepted
tradeoff, not a correctness bug (rate limiting doesn't touch seat-booking correctness, which is
the thing this project's "no in-memory state" rule actually protects). Verified live: 7 rapid
login attempts against a limit of 5/min returned `401, 401, 401, 429, 429, 429, 429` — the exact
cutoff, accounting for 2 earlier auth calls already counted in that same window.

Later stages will add k6 load tests and the full README (live demo URL, demo credentials,
measured benchmark results).
