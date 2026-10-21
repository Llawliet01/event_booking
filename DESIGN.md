# Design

## Problem

Many users can try to book the same seat at the same event at the same instant. Exactly one
of them must win — even across retries, crashes, and 500+ concurrent requests, and even with
multiple API instances running behind a load balancer with no shared memory between them.
Nothing in-process (a Python lock, an in-memory dict, a per-instance cache) can provide that
guarantee once there's more than one process involved, so the guarantee has to live somewhere
every instance already shares: the database.

## Why the naive version fails

Stage 2 (`app/bookings/repository.py` as of commit `1843791`) implemented booking creation as
two separate statements: `SELECT` to check whether the seat is free, then `INSERT` the hold.
Nothing synchronizes those two statements against a concurrent request doing the same thing.
Two requests can both run the `SELECT`, both see zero existing bookings, and both proceed to
`INSERT` — neither has any way to know about the other until it's too late.

This isn't a hypothetical. Migration `0002` deliberately dropped the one constraint
(`uq_bookings_active_seat`) that was backstopping it, and `scripts/naive_race_test.py` fired
200 concurrent requests at one seat. Measured result: **10 successful bookings on one seat**,
not 1 — exactly `POOL_MAX` (the connection pool size), because all 10 pooled connections picked
up a request at nearly the same instant and each ran its own `SELECT`-sees-nothing,
`INSERT`-succeeds sequence independently.

## How the fix works

Stage 3 restored the constraint (migration `0003`) and replaced the naive logic with two
interchangeable strategies, chosen by `BOOKING_STRATEGY`:

- **Strategy A — partial unique index.** `uq_bookings_active_seat` is a unique index on
  `bookings(seat_id)` scoped to `WHERE status IN ('held','pending_payment','confirmed')`. A
  hold attempt is one transaction: expire this seat's stale holds, then insert. If a concurrent
  request already won and committed, our insert violates that index and Postgres raises a
  unique-violation — we catch it and report 409. This is optimistic: everyone tries the insert,
  the database decides who wins.
- **Strategy B — `SELECT ... FOR UPDATE`.** Locks the seat row first, so every concurrent
  attempt on that seat serializes: only one transaction at a time can even reach the insert.
  This is pessimistic: lock first, so there's nothing left to race by the time anyone inserts.

Neither strategy uses an app-level lock, a global lock, or in-memory state of any kind — the
guarantee is enforced by Postgres itself, which is why it holds regardless of how many API
instances are running. Re-running the identical 200-concurrent-request proof against both
strategies: **exactly 1 success, 199 conflicts, every run**, for both A and B.

The same "the database enforces this even if the application is wrong" principle extends past
booking creation: `chk_hold_expiry_required` makes it impossible to insert a `held` or
`pending_payment` row without an expiry, and the `idempotency_keys` and `webhook_events`
primary keys make double-claiming a key or double-processing a webhook event a constraint
violation, not just an application convention.

## Isolation level choice

Everything in this project runs under Postgres's default, `READ COMMITTED`. That's a
deliberate choice, not an oversight: every place that needs a stronger guarantee gets it from
an explicit mechanism (a unique index, a row lock via `FOR UPDATE`, a CAS `UPDATE` with a
`WHERE` clause that re-checks the condition at write time) rather than from the isolation
level. `SERIALIZABLE` would also prevent the double-booking race, but at the cost of needing
retry-on-serialization-failure logic everywhere and extra contention under load, to buy a
guarantee the unique index and row locks already provide for free. The one place this
deserves a second look is the idempotency-key resolve loop
(`app/idempotency/repository.py`), which explicitly depends on `READ COMMITTED`'s
per-statement snapshot: it re-reads the key's row mid-transaction specifically to observe
another transaction's commit (a reclaim or completion) that happened between its own
statements. Under `REPEATABLE READ`, that re-read would still see the original snapshot from
the start of the transaction, and the retry loop would never observe the row changing underneath it.

## Failure scenarios and how they're handled

- **Two requests race for the same seat.** Handled by the unique index (Strategy A) or the row
  lock (Strategy B) — see above. Proven with a live 200-concurrent-request run against both.
- **A hold's 5-minute window lapses before the holder pays.** No cron job required: every hold
  attempt (`_expire_stale_holds`) and every seat-availability read
  (`app/events/repository.py`) independently treats `hold_expires_at <= now()` as free.
  `app/jobs/release_expired_holds.py` exists purely to shrink how many expired rows accumulate
  — correctness never depends on it running. Proven by force-expiring a hold directly in
  Postgres and watching both the availability query and a second user's booking attempt treat
  it as free with no cleanup job involved.
- **The same client retries an identical request** (network blip, double-click, at-least-once
  delivery). `app/idempotency/repository.py` claims the `Idempotency-Key` in its own committed
  transaction before running the handler; a retry with the same key and body gets the
  originally-computed response replayed as `200`, never re-running the side effect.
- **Two different requests reuse the same key with different bodies.** Rejected with `422`;
  the original completed record is left untouched.
- **A request crashes after claiming an idempotency key but before completing.** The key is
  left `in_progress`. A fresh retry within `IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS` correctly
  sees it as still-genuinely-running (`409`); after that window, a retry reclaims it via a CAS
  `UPDATE` that only succeeds if the row is still `in_progress` and still stale at that exact
  moment, so a race between two would-be reclaimers resolves to exactly one winner.
- **A payment succeeds, but the hold had already lapsed by the time the webhook arrives.** The
  booking is deliberately *not* confirmed — the seat may already be re-sold. The payment is
  recorded with `needs_review = true, refund_status = 'pending'` instead, synchronously, inside
  the same transaction that discovers the mismatch. Proven live: forced a hold into the past,
  sent a validly-signed `succeeded` webhook, and confirmed the booking stayed `pending_payment`
  while the payment landed flagged for review.
- **A webhook is delivered twice, or two distinct payment events land out of order for the same
  booking.** `webhook_events.event_id` is a primary key, so a replay of the exact same event is
  a no-op (dedupe proven live: identical event posted twice, `webhook_events` and `payments`
  both show exactly one row). Two genuinely different events (e.g. a `failed` event followed
  later by an unrelated `succeeded` one) are each recorded faithfully, but the booking's status
  only moves when the guard (`status = 'pending_payment'`) still holds — proven live by sending
  a `failed` event that cancelled a booking, then letting the automatic simulator's own
  `succeeded` callback for the same booking arrive afterward and confirming it did *not*
  un-cancel it.
- **A crash happens between a payment being recorded and the booking being confirmed** (not the
  lapsed-hold case above — a true gap, e.g. an application bug or an interrupted deploy).
  `app/bookings/reconciliation.py` doesn't assume a specific failure point; it re-derives the
  invariant "a succeeded payment implies either a confirmed booking or an explicit
  `needs_review` flag" and fixes any violation it finds. Proven live by manually constructing
  exactly that inconsistent state (bypassing the webhook handler entirely) and confirming
  `reconcile_payments()` finds and flags it, then is a no-op on a second pass.
- **Any step in a transaction raises.** Every write path in this project wraps its statements
  in `async with conn.transaction():`; an exception anywhere inside rolls back everything in
  that block, including any dedupe/claim row inserted earlier in the same block. Nothing is
  ever half-created.

## What I would do at scale

- **Replace the lazy-expiry-only model with a real background sweeper** once a job-queue
  project exists to plug `release_expired_holds()` and `cleanup_old_idempotency_keys()` into —
  they're already written as plain functions with no scheduler dependency for exactly this.
- **Move `BOOKING_STRATEGY` from a global config flag to a per-event or per-load-pattern
  choice.** Strategy A (optimistic) and Strategy B (pessimistic) have different contention
  behavior under extreme hotspot load (one seat, massive concurrency) versus spread-out load
  (many seats, moderate concurrency); the k6 results in `/benchmarks` are the starting point
  for that decision, not the end of it.
- **Shard or partition `bookings` and `idempotency_keys` by event** if a single event's seat
  count and request volume ever outgrow one Postgres instance's write throughput — the current
  schema doesn't need this, but the partial unique index and row-lock strategies both still
  work unmodified per-shard.
- **Replace the in-process HTTP-based payment simulator with a real provider integration and a
  real outbox pattern** for the webhook-processing transaction, so "record the payment" and
  "notify downstream systems" can't drift apart under partial failure the way an in-process
  `BackgroundTasks` call could.
- **Add a read replica for `GET /events/{id}/seats`** once availability reads start competing
  with booking writes for connections — the query is already pure `SELECT`, so routing it to a
  replica needs no logic change, just a second connection pool.

## Tradeoffs I chose

<!-- Fill this in yourself. -->
