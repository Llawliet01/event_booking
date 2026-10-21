"""Simulates an external payment provider's asynchronous callback.

Real providers (Stripe etc.) charge a card, then call your webhook some
time later over the open internet, signed so you can trust it came from
them. This fakes that shape in-process: schedule_payment_callback() is
handed to FastAPI's BackgroundTasks from POST /bookings/{id}/pay, waits
briefly, then HTTP-POSTs a signed event to our own /webhooks/payment --
the same path a real provider's callback would take from the outside.
No Celery/Redis: this is a demo-scoped simulator, not production payment
infrastructure.
"""
import asyncio
import json
import uuid

import httpx

from app.webhooks.security import sign


async def schedule_payment_callback(
    base_url: str,
    secret: str,
    booking_id: int,
    amount_cents: int,
    outcome: str = "succeeded",
    delay_seconds: float = 1.0,
) -> None:
    await asyncio.sleep(delay_seconds)
    payload = {
        "event_id": str(uuid.uuid4()),
        "booking_id": booking_id,
        "amount_cents": amount_cents,
        "status": outcome,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    signature = sign(body, secret)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(
                f"{base_url}/webhooks/payment",
                content=body,
                headers={"Content-Type": "application/json", "X-Signature": signature},
            )
        except httpx.HTTPError:
            pass  # best-effort simulator; a real provider would retry on failure
