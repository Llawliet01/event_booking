"""The single source of truth for which booking status transitions are
legal. Every place that changes a booking's status (cancel, pay, webhook
confirm) must check this before writing, so the rule lives in exactly one
function instead of being re-derived ad hoc at each call site.
"""

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "held": {"pending_payment", "expired", "cancelled"},
    "pending_payment": {"confirmed", "expired", "cancelled"},
    "confirmed": {"cancelled"},
    "expired": set(),
    "cancelled": set(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, set())
