from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel

from app.auth.dependencies import get_current_user_id
from app.bookings.repository import (
    Booking,
    BookingNotFoundError,
    InvalidTransitionError,
    NotOwnerError,
    SeatNotFoundError,
    SeatTakenError,
    cancel_booking,
    create_hold,
    list_bookings_for_user,
    start_payment,
)
from app.config import get_settings
from app.db import get_conn
from app.idempotency.repository import (
    Outcome,
    begin_idempotent_request,
    complete_idempotent_request,
    compute_request_hash,
)
from app.observability.metrics import bookings_conflict_total, bookings_success_total
from app.payments.provider_simulator import schedule_payment_callback

router = APIRouter(prefix="/bookings", tags=["bookings"])


class CreateBookingRequest(BaseModel):
    seat_id: int


class BookingResponse(BaseModel):
    id: int
    seat_id: int
    user_id: int
    status: str

    @classmethod
    def from_booking(cls, booking: Booking) -> "BookingResponse":
        return cls(
            id=booking.id,
            seat_id=booking.seat_id,
            user_id=booking.user_id,
            status=booking.status,
        )


@router.post("", status_code=201)
async def create_booking(
    body: CreateBookingRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user_id: int = Depends(get_current_user_id),
    conn: AsyncConnection = Depends(get_conn),
) -> JSONResponse:
    """Stage 5: Idempotency-Key is required. A missing header is rejected
    by FastAPI's own header validation before this body runs (422).

    The key is claimed in its own committed transaction *before* the hold
    is attempted (app/idempotency/repository.py), so a crash partway
    through leaves the key 'in_progress' for the stale-reclaim path to pick
    up later, rather than silently forgetting the attempt ever happened.

    A replay (same key, same body) always returns 200, even if the
    original attempt resulted in a 409/404 -- 200 here means "here is the
    cached result of a request that already ran," not "this succeeded."
    """
    settings = get_settings()
    request_hash = compute_request_hash(body.model_dump())

    result = await begin_idempotent_request(
        conn,
        user_id,
        idempotency_key,
        request_hash,
        settings.IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS,
    )

    if result.outcome is Outcome.REPLAY:
        return JSONResponse(status_code=200, content=result.response_body)
    if result.outcome is Outcome.MISMATCH:
        raise HTTPException(
            status_code=422, detail="Idempotency-Key reused with a different request body"
        )
    if result.outcome is Outcome.IN_PROGRESS:
        raise HTTPException(
            status_code=409, detail="a request with this Idempotency-Key is already in progress"
        )

    # Outcome.CLAIMED: we own this key. Run the real operation, map any
    # business-rule rejection to its response, and persist whatever we're
    # about to return so a retry with this same key replays it instead of
    # re-running create_hold.
    try:
        booking = await create_hold(
            conn,
            user_id,
            body.seat_id,
            settings.HOLD_DURATION_MINUTES,
            settings.BOOKING_STRATEGY,
        )
        response_code = 201
        response_body = BookingResponse.from_booking(booking).model_dump(mode="json")
        bookings_success_total.inc()
    except SeatTakenError:
        response_code = 409
        response_body = {"detail": "seat already taken"}
        bookings_conflict_total.inc()
    except SeatNotFoundError:
        response_code = 404
        response_body = {"detail": "seat not found"}

    await complete_idempotent_request(conn, user_id, idempotency_key, response_code, response_body)
    return JSONResponse(status_code=response_code, content=response_body)


@router.post("/{booking_id}/pay", response_model=BookingResponse)
async def pay(
    booking_id: int,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user_id),
    conn: AsyncConnection = Depends(get_conn),
) -> BookingResponse:
    """held -> pending_payment, then schedules a simulated provider
    callback (a real HTTP POST to our own /webhooks/payment, signed the
    same way a real provider's callback would be) to land shortly after
    this response. No Idempotency-Key here -- only POST /bookings needs
    one per the spec.
    """
    settings = get_settings()
    try:
        booking = await start_payment(conn, booking_id, user_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="booking not found")
    except NotOwnerError:
        raise HTTPException(status_code=403, detail="not your booking")
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(
        schedule_payment_callback,
        base_url=f"http://127.0.0.1:{settings.PORT}",
        secret=settings.WEBHOOK_HMAC_SECRET,
        booking_id=booking.id,
        amount_cents=settings.PAYMENT_AMOUNT_CENTS,
        outcome="succeeded",
        delay_seconds=settings.PAYMENT_SIMULATOR_DELAY_SECONDS,
    )
    return BookingResponse.from_booking(booking)


@router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel(
    booking_id: int,
    user_id: int = Depends(get_current_user_id),
    conn: AsyncConnection = Depends(get_conn),
) -> BookingResponse:
    try:
        booking = await cancel_booking(conn, booking_id, user_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="booking not found")
    except NotOwnerError:
        raise HTTPException(status_code=403, detail="not your booking")
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return BookingResponse.from_booking(booking)


@router.get("/me", response_model=list[BookingResponse])
async def list_my_bookings(
    user_id: int = Depends(get_current_user_id),
    conn: AsyncConnection = Depends(get_conn),
) -> list[BookingResponse]:
    bookings = await list_bookings_for_user(conn, user_id)
    return [BookingResponse.from_booking(b) for b in bookings]
