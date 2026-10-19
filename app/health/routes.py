from fastapi import APIRouter, HTTPException

from app.db import get_pool

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    # Cheap and DB-free: this is what the hosting platform pings to decide
    # if the process is alive at all.
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready() -> dict[str, str]:
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}
