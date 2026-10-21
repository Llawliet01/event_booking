from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.db import get_pool
from app.observability.metrics import db_pool_in_use, db_pool_size

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus text format. Gauges are set here, at scrape time, from
    the live pool stats -- never stale between scrapes. Must be kept off
    the public internet in deployment (see README); nothing here enforces
    that at the app layer, it's an infra-level concern.
    """
    stats = get_pool().get_stats()
    db_pool_size.set(stats.get("pool_size", 0))
    db_pool_in_use.set(stats.get("pool_size", 0) - stats.get("pool_available", 0))
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
