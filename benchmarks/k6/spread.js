// 500 VUs, one booking attempt each, spread across 100 seats (5 VUs per
// seat). Run scripts/k6_setup.py first to generate fixtures.json.
import http from 'k6/http';
import { check } from 'k6';

const fixtures = JSON.parse(open('./fixtures.json'));
const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = {
  scenarios: {
    spread: {
      executor: 'per-vu-iterations',
      vus: __ENV.VUS ? parseInt(__ENV.VUS, 10) : 500,
      iterations: 1,
      maxDuration: '30s',
    },
  },
};

export default function () {
  const seatId = fixtures.spread_seat_ids[(__VU - 1) % fixtures.spread_seat_ids.length];
  const token = fixtures.tokens[(__VU - 1) % fixtures.tokens.length];
  const idempotencyKey = `spread-${__VU}-${__ITER}-${Date.now()}`;
  const res = http.post(
    `${BASE_URL}/bookings`,
    JSON.stringify({ seat_id: seatId }),
    {
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        'Idempotency-Key': idempotencyKey,
      },
    }
  );
  check(res, {
    'status is 201 or 409': (r) => r.status === 201 || r.status === 409,
  });
}
