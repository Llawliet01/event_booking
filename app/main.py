from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import router as auth_router
from app.bookings.routes import router as bookings_router
from app.config import get_settings
from app.db import close_pool, open_pool
from app.events.routes import router as events_router
from app.health.routes import router as health_router
from app.webhooks.routes import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    yield
    await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Event Booking API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.CORS_ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(events_router)
    app.include_router(bookings_router)
    app.include_router(webhooks_router)
    return app


app = create_app()
