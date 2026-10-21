from fastapi import APIRouter, Depends, HTTPException
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
)
from app.config import get_settings
from app.db import get_conn

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


@router.post("", status_code=201, response_model=BookingResponse)
async def create_booking(
    body: CreateBookingRequest,
    user_id: int = Depends(get_current_user_id),
    conn: AsyncConnection = Depends(get_conn),
) -> BookingResponse:
    """Stage 4: the caller's identity now comes from their JWT, not the
    request body. Idempotency-Key handling arrives in Stage 5.
    """
    settings = get_settings()
    try:
        booking = await create_hold(
            conn,
            user_id,
            body.seat_id,
            settings.HOLD_DURATION_MINUTES,
            settings.BOOKING_STRATEGY,
        )
    except SeatTakenError:
        raise HTTPException(status_code=409, detail="seat already taken")
    except SeatNotFoundError:
        raise HTTPException(status_code=404, detail="seat not found")
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
