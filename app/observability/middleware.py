import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.observability.metrics import http_request_duration_seconds

logger = structlog.get_logger()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns/propagates a request id, times the request, and logs one
    structured line per request -- method/path/status/duration only, never
    headers or bodies, so auth tokens and passwords never reach the logs.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        response.headers["X-Request-ID"] = request_id
        http_request_duration_seconds.labels(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        ).observe(duration)

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        return response
