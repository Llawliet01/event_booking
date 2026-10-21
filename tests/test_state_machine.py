"""Pure unit tests for the booking state machine -- no DB needed.

Exhaustively checks every (from_status, to_status) pair across all five
statuses against the transition table in app/bookings/state_machine.py.
"""

import pytest

from app.bookings.state_machine import ALLOWED_TRANSITIONS, can_transition

ALL_STATUSES = ["held", "pending_payment", "confirmed", "expired", "cancelled"]

ALLOWED_PAIRS = {
    (from_status, to_status)
    for from_status, to_statuses in ALLOWED_TRANSITIONS.items()
    for to_status in to_statuses
}

ALL_PAIRS = [
    (from_status, to_status) for from_status in ALL_STATUSES for to_status in ALL_STATUSES
]


@pytest.mark.parametrize("from_status,to_status", ALL_PAIRS)
def test_transition_matches_table(from_status: str, to_status: str) -> None:
    expected = (from_status, to_status) in ALLOWED_PAIRS
    assert can_transition(from_status, to_status) is expected


def test_terminal_states_allow_nothing() -> None:
    for terminal in ("expired", "cancelled"):
        for to_status in ALL_STATUSES:
            assert can_transition(terminal, to_status) is False


def test_unknown_from_status_allows_nothing() -> None:
    for to_status in ALL_STATUSES:
        assert can_transition("not_a_real_status", to_status) is False
