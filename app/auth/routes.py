from fastapi import APIRouter, Depends, HTTPException
from psycopg import AsyncConnection, errors

from app.auth.rate_limit import rate_limit_auth
from app.auth.schemas import LoginRequest, RegisterRequest, TokenResponse
from app.auth.security import create_access_token, hash_password, verify_password
from app.db import get_conn

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(rate_limit_auth)])


@router.post("/register", status_code=201, response_model=TokenResponse)
async def register(
    body: RegisterRequest, conn: AsyncConnection = Depends(get_conn)
) -> TokenResponse:
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (email, password_hash)
                    VALUES (%(email)s, %(password_hash)s)
                    RETURNING id
                    """,
                    {"email": body.email, "password_hash": hash_password(body.password)},
                )
                row = await cur.fetchone()
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="email already registered")
    return TokenResponse(access_token=create_access_token(row[0]))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, conn: AsyncConnection = Depends(get_conn)
) -> TokenResponse:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, password_hash FROM users WHERE email = %(email)s",
            {"email": body.email},
        )
        row = await cur.fetchone()

    if row is None or not verify_password(body.password, row[1]):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return TokenResponse(access_token=create_access_token(row[0]))
