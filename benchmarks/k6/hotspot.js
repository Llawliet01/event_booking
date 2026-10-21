// 300 VUs, one booking attempt each, all targeting the same seat.
// Run scripts/k6_setup.py first to generate fixtures.json.
import http from 'k6/http';
import { check } from 'k6';

const fixtures = JSON.parse(open('./fixtures.json'));
const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = {
  scenarios: {
    hotspot: {
      executor: 'per-vu-iterations',
      vus: __ENV.VUS ? parseInt(__ENV.VUS, 10) : 300,
      iterations: 1,
      maxDuration: '30s',
    },
  },
};

export default function () {
  const token = fixtures.tokens[(__VU - 1) % fixtures.tokens.length];
  const idempotencyKey = `hotspot-${__VU}-${__ITER}-${Date.now()}`;
  const res = http.post(
    `${BASE_URL}/bookings`,
    JSON.stringify({ seat_id: fixtures.hotspot_seat_id }),
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
