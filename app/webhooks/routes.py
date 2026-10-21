from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg import AsyncConnection

from app.config import get_settings
from app.db import get_conn
from app.payments.repository import BookingNotFoundError, process_payment_webhook
from app.webhooks.security import verify

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payment", status_code=200)
async def payment_webhook(
    request: Request,
    x_signature: str = Header(..., alias="X-Signature"),
    conn: AsyncConnection = Depends(get_conn),
) -> dict:
    """The signature must be verified over the exact raw bytes the sender
    signed, so we read the raw body first and only parse JSON after
    verification -- letting FastAPI auto-parse into a model first would
    risk re-serializing with different whitespace/key order and breaking
    the signature check.
    """
    settings = get_settings()
    raw_body = await request.body()
    if not verify(raw_body, x_signature, settings.WEBHOOK_HMAC_SECRET):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    try:
        event_id = str(payload["event_id"])
        booking_id = int(payload["booking_id"])
        amount_cents = int(payload["amount_cents"])
        status = payload["status"]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="malformed webhook payload")
    if status not in ("succeeded", "failed"):
        raise HTTPException(status_code=422, detail="unknown payment status")

    try:
        await process_payment_webhook(conn, event_id, payload, booking_id, amount_cents, status)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="booking not found")
    return {"status": "ok"}
