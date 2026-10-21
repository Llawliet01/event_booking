import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from psycopg import AsyncConnection
from pydantic import BaseModel

from app.db import get_conn
from app.events.repository import event_exists, list_events, list_seat_availability

router = APIRouter(tags=["events"])


class EventResponse(BaseModel):
    id: int
    name: str
    starts_at: dt.datetime


class SeatAvailabilityResponse(BaseModel):
    id: int
    label: str
    availability: str


@router.get("/events", response_model=list[EventResponse])
async def get_events(conn: AsyncConnection = Depends(get_conn)) -> list[EventResponse]:
    events = await list_events(conn)
    return [EventResponse(id=e.id, name=e.name, starts_at=e.starts_at) for e in events]


@router.get("/events/{event_id}/seats", response_model=list[SeatAvailabilityResponse])
async def get_event_seats(
    event_id: int, conn: AsyncConnection = Depends(get_conn)
) -> list[SeatAvailabilityResponse]:
    if not await event_exists(conn, event_id):
        raise HTTPException(status_code=404, detail="event not found")
    seats = await list_seat_availability(conn, event_id)
    return [
        SeatAvailabilityResponse(id=s.id, label=s.label, availability=s.availability)
        for s in seats
    ]
