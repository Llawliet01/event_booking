"""Pre-generates fixtures for the k6 load tests in benchmarks/k6/: users
(with JWTs minted directly, bypassing HTTP registration since 300-500
concurrent /auth/register calls would immediately hit the per-instance
auth rate limit), one single-seat event (hotspot scenario) and one
100-seat event (spread scenario).

Writes benchmarks/k6/fixtures.json, which the k6 scripts read via open()
at init time. Not committed to git (contains live JWTs, regenerated
fresh for every run).

Usage: python scripts/k6_setup.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

from app.auth.security import create_access_token

DATABASE_URL = os.environ["DATABASE_URL"]
N_USERS = 500  # covers both the 300-VU and 500-VU scenarios
RUN_ID = int(time.time())


def main() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s)",
                [(f"k6-{RUN_ID}-{i}@example.com", "x") for i in range(N_USERS)],
            )
            cur.execute(
                "SELECT id FROM users WHERE email LIKE %s ORDER BY id",
                (f"k6-{RUN_ID}-%@example.com",),
            )
            user_ids = [r[0] for r in cur.fetchall()]

            cur.execute(
                "INSERT INTO events (name, starts_at) VALUES (%s, now() + interval '1 day') RETURNING id",
                (f"k6 hotspot {RUN_ID}",),
            )
            hotspot_event_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO seats (event_id, label) VALUES (%s, 'HOT') RETURNING id",
                (hotspot_event_id,),
            )
            hotspot_seat_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO events (name, starts_at) VALUES (%s, now() + interval '1 day') RETURNING id",
                (f"k6 spread {RUN_ID}",),
            )
            spread_event_id = cur.fetchone()[0]
            cur.executemany(
                "INSERT INTO seats (event_id, label) VALUES (%s, %s)",
                [(spread_event_id, f"S{i}") for i in range(100)],
            )
            cur.execute("SELECT id FROM seats WHERE event_id = %s ORDER BY id", (spread_event_id,))
            spread_seat_ids = [r[0] for r in cur.fetchall()]

    tokens = [create_access_token(uid) for uid in user_ids]

    fixtures = {
        "hotspot_seat_id": hotspot_seat_id,
        "spread_seat_ids": spread_seat_ids,
        "tokens": tokens,
    }
    out_path = os.path.join(os.path.dirname(__file__), "..", "benchmarks", "k6", "fixtures.json")
    with open(out_path, "w") as f:
        json.dump(fixtures, f)

    print(
        f"Wrote {len(tokens)} tokens, hotspot_seat_id={hotspot_seat_id}, "
        f"{len(spread_seat_ids)} spread seats -> {out_path}"
    )


if __name__ == "__main__":
    main()
