"""Idempotency-key claim/replay logic for POST /bookings, keyed by (user_id, key).

begin_idempotent_request() is the only entry point:

  1. INSERT ... ON CONFLICT (user_id, key) DO NOTHING RETURNING * -- if a row
     gets inserted, we own this key: the caller should run its handler and
     then call complete_idempotent_request() with the outcome.
  2. No row inserted -> a row already exists. Resolve it:
       completed, same request_hash   -> REPLAY (caller returns 200 with the
                                          stored response, never re-running
                                          the handler)
       completed, different hash      -> MISMATCH (422; the existing
                                          completed record is left alone)
       in_progress, still fresh       -> IN_PROGRESS (409; another request
                                          with this key is genuinely still
                                          running)
       in_progress, stale            -> reclaim it via a CAS UPDATE (only
                                          succeeds if it's still in_progress
                                          AND still stale at the moment of
                                          the UPDATE). Success means we now
                                          own it, same as case 1. Failure
                                          means another request reclaimed or
                                          completed it in the meantime, so we
                                          loop and re-resolve from the top.

"Stale" means updated_at is older than IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS
-- i.e. the original attempt most likely crashed before marking the key
completed, not that it's merely slow. On an unhandled exception in the
caller's handler, nothing here marks the key completed, so it is left
in_progress for exactly this mechanism to reclaim later.
"""
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

_MAX_RESOLVE_ATTEMPTS = 5


def compute_request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Outcome(Enum):
    CLAIMED = "claimed"
    REPLAY = "replay"
    MISMATCH = "mismatch"
    IN_PROGRESS = "in_progress"


@dataclass
class IdempotencyResult:
    outcome: Outcome
    response_code: int | None = None
    response_body: dict | None = None


async def _try_claim(cur, user_id: int, key: str, request_hash: str) -> bool:
    await cur.execute(
        """
        INSERT INTO idempotency_keys (user_id, key, request_hash, status)
        VALUES (%(user_id)s, %(key)s, %(request_hash)s, 'in_progress')
        ON CONFLICT (user_id, key) DO NOTHING
        RETURNING user_id
        """,
        {"user_id": user_id, "key": key, "request_hash": request_hash},
    )
    return await cur.fetchone() is not None


async def begin_idempotent_request(
    conn: AsyncConnection,
    user_id: int,
    key: str,
    request_hash: str,
    in_progress_timeout_seconds: int,
) -> IdempotencyResult:
    async with conn.transaction():
        async with conn.cursor() as cur:
            if await _try_claim(cur, user_id, key, request_hash):
                return IdempotencyResult(outcome=Outcome.CLAIMED)

            for _ in range(_MAX_RESOLVE_ATTEMPTS):
                await cur.execute(
                    """
                    SELECT request_hash, status, response_code, response_body,
                           (now() - updated_at) >= (%(timeout)s * interval '1 second') AS is_stale
                    FROM idempotency_keys
                    WHERE user_id = %(user_id)s AND key = %(key)s
                    """,
                    {"user_id": user_id, "key": key, "timeout": in_progress_timeout_seconds},
                )
                existing_hash, status, response_code, response_body, is_stale = await cur.fetchone()

                if status == "completed":
                    if existing_hash != request_hash:
                        return IdempotencyResult(outcome=Outcome.MISMATCH)
                    return IdempotencyResult(
                        outcome=Outcome.REPLAY,
                        response_code=response_code,
                        response_body=response_body,
                    )

                # status == "in_progress": try to reclaim, but only if our
                # own read saw it as stale -- otherwise it's a genuine
                # concurrent duplicate and there's nothing to retry.
                if is_stale:
                    await cur.execute(
                        """
                        UPDATE idempotency_keys
                        SET request_hash = %(request_hash)s, updated_at = now()
                        WHERE user_id = %(user_id)s AND key = %(key)s
                          AND status = 'in_progress'
                          AND (now() - updated_at) >= (%(timeout)s * interval '1 second')
                        RETURNING user_id
                        """,
                        {
                            "user_id": user_id,
                            "key": key,
                            "request_hash": request_hash,
                            "timeout": in_progress_timeout_seconds,
                        },
                    )
                    if await cur.fetchone() is not None:
                        return IdempotencyResult(outcome=Outcome.CLAIMED)
                    # Reclaim lost a race with another request between our
                    # SELECT and this UPDATE -- loop and re-resolve fresh.
                    continue

                return IdempotencyResult(outcome=Outcome.IN_PROGRESS)

    # Exhausted retries under heavy contention on this exact key; the safe
    # default is to tell this caller to retry rather than guess.
    return IdempotencyResult(outcome=Outcome.IN_PROGRESS)


async def complete_idempotent_request(
    conn: AsyncConnection,
    user_id: int,
    key: str,
    response_code: int,
    response_body: dict,
) -> None:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE idempotency_keys
                SET status = 'completed',
                    response_code = %(response_code)s,
                    response_body = %(response_body)s,
                    updated_at = now()
                WHERE user_id = %(user_id)s AND key = %(key)s
                """,
                {
                    "user_id": user_id,
                    "key": key,
                    "response_code": response_code,
                    "response_body": Jsonb(response_body),
                },
            )
