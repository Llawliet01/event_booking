"""Seed demo data: one user, one event, 50 seats. Safe to re-run (idempotent)."""

import os

import psycopg
from argon2 import PasswordHasher

DATABASE_URL = os.environ["DATABASE_URL"]
DEMO_EMAIL = os.environ.get("DEMO_USER_EMAIL", "demo@example.com")
DEMO_PASSWORD = os.environ.get("DEMO_USER_PASSWORD", "demo12345")
EVENT_NAME = "Demo Concert"

hasher = PasswordHasher()


def main() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, password_hash)
                VALUES (%s, %s)
                ON CONFLICT (email) DO NOTHING
                """,
                (DEMO_EMAIL, hasher.hash(DEMO_PASSWORD)),
            )

            cur.execute("SELECT id FROM events WHERE name = %s", (EVENT_NAME,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO events (name, starts_at)
                    VALUES (%s, now() + interval '30 days')
                    RETURNING id
                    """,
                    (EVENT_NAME,),
                )
                row = cur.fetchone()
            event_id = row[0]

            cur.executemany(
                """
                INSERT INTO seats (event_id, label)
                VALUES (%s, %s)
                ON CONFLICT (event_id, label) DO NOTHING
                """,
                [(event_id, f"S{i}") for i in range(1, 51)],
            )

    print(f"Seeded demo user ({DEMO_EMAIL}), event_id={event_id}, 50 seats.")


if __name__ == "__main__":
    main()
