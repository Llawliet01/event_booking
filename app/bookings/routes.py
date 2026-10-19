from fastapi import APIRouter, Depends, HTTPException
from psycopg import AsyncConnection
from pydantic import BaseModel

from app.bookings.repository import (
    Booking,
    SeatNotFoundError,
    SeatTakenError,
    create_hold,
)
from app.config import get_settings
from app.db import get_conn

router = APIRouter(prefix="/bookings", tags=["bookings"])


class CreateBookingRequest(BaseModel):
    user_id: int
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


@router.post("", status_code=201, response_model=BookingResponse)
async def create_booking(
    body: CreateBookingRequest,
    conn: AsyncConnection = Depends(get_conn),
) -> BookingResponse:
    """Stage 3: concurrency-safe hold creation via the configured strategy
    (Settings.BOOKING_STRATEGY). Still no auth, Idempotency-Key, or state
    machine -- those arrive in later stages.
    """
    settings = get_settings()
    try:
        booking = await create_hold(
            conn,
            body.user_id,
            body.seat_id,
            settings.HOLD_DURATION_MINUTES,
            settings.BOOKING_STRATEGY,
        )
    except SeatTakenError:
        raise HTTPException(status_code=409, detail="seat already taken")
    except SeatNotFoundError:
        raise HTTPException(status_code=404, detail="seat not found")
    return BookingResponse.from_booking(booking)
