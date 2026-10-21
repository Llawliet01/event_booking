"""Prometheus metrics, scraped at GET /metrics (app/observability/routes.py).

bookings_success_total / bookings_conflict_total are incremented right
where POST /bookings already knows the outcome (app/bookings/routes.py).
db_pool_size / db_pool_in_use are set at scrape time from the live pool
stats rather than on a timer, so they're never stale between scrapes.
"""
from prometheus_client import Counter, Gauge, Histogram

bookings_success_total = Counter(
    "bookings_success_total", "Successful booking holds created"
)
bookings_conflict_total = Counter(
    "bookings_conflict_total", "Booking attempts rejected because the seat was already taken"
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path", "status_code"],
)
db_pool_size = Gauge(
    "db_pool_connections_total", "Total connections currently open in the pool"
)
db_pool_in_use = Gauge(
    "db_pool_connections_in_use", "Connections currently checked out of the pool"
)
