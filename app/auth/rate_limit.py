"""Fixed-window rate limiter for /auth/register and /auth/login.

Deliberately per-instance, in-memory: this project has no Redis (see
project constraints in README/DESIGN.md), so a distributed limiter is out
of scope -- each API instance enforces its own window independently,
which is an accepted tradeoff, not a correctness bug. This is unrelated
to the "never use in-memory state for correctness" rule elsewhere in this
codebase: that rule is about seat-booking correctness, which this
in-memory state never touches.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.config import get_settings

_WINDOW_SECONDS = 60.0
_hits: dict[str, deque] = defaultdict(deque)


async def rate_limit_auth(request: Request) -> None:
    settings = get_settings()
    limit = settings.AUTH_RATE_LIMIT_PER_MINUTE
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    hits = _hits[key]

    while hits and now - hits[0] > _WINDOW_SECONDS:
        hits.popleft()

    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="too many requests, try again later")

    hits.append(now)
