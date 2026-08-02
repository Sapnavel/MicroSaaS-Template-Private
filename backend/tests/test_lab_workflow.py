"""Pure, DB-independent unit tests for `services/lab_workflow.py`
(PRPs/lab-module-prp.md Phase 3 / Validation Gate 1: "the transition table
itself is unit-testable with no DB fixture"). No `db`/`client` fixtures are
used anywhere in this file -- these tests exercise `is_legal_transition` and
`validate_result_for_transition` directly, both pure functions per that
module's docstring.
"""

import itertools

import pytest

from app.models.lab import LabOrderStatus
from app.services.lab_workflow import is_legal_transition, validate_result_for_transition

ALL_STATUSES = list(LabOrderStatus)

# The only four legal forward steps in the chain
# ordered -> collected -> processing -> verified -> attached.
LEGAL_TRANSITIONS = {
    (LabOrderStatus.ordered, LabOrderStatus.collected),
    (LabOrderStatus.collected, LabOrderStatus.processing),
    (LabOrderStatus.processing, LabOrderStatus.verified),
    (LabOrderStatus.verified, LabOrderStatus.attached),
}

# Every other (current, requested) pair among the 5 statuses -- skips,
# backward moves, repeats, and anything from the terminal `attached` state.
ALL_PAIRS = set(itertools.product(ALL_STATUSES, ALL_STATUSES))
ILLEGAL_TRANSITIONS = ALL_PAIRS - LEGAL_TRANSITIONS


# ---------------------------------------------------------------------------
# is_legal_transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current,requested", sorted(LEGAL_TRANSITIONS, key=str))
def test_is_legal_transition_true_for_every_legal_forward_step(current, requested):
    assert is_legal_transition(current, requested) is True


@pytest.mark.parametrize("current,requested", sorted(ILLEGAL_TRANSITIONS, key=str))
def test_is_legal_transition_false_for_every_illegal_pair(current, requested):
    """Covers, by construction, every skip (e.g. ordered->processing), every
    backward move (e.g. verified->collected), every repeat of the current
    status (e.g. collected->collected), and every transition attempted from
    the terminal `attached` status."""
    assert is_legal_transition(current, requested) is False


def test_is_legal_transition_skip_a_step_rejected():
    assert is_legal_transition(LabOrderStatus.ordered, LabOrderStatus.processing) is False
    assert is_legal_transition(LabOrderStatus.ordered, LabOrderStatus.verified) is False
    assert is_legal_transition(LabOrderStatus.ordered, LabOrderStatus.attached) is False
    assert is_legal_transition(LabOrderStatus.collected, LabOrderStatus.attached) is False


def test_is_legal_transition_backward_move_rejected():
    assert is_legal_transition(LabOrderStatus.collected, LabOrderStatus.ordered) is False
    assert is_legal_transition(LabOrderStatus.attached, LabOrderStatus.verified) is False


def test_is_legal_transition_repeat_current_status_rejected():
    for st in ALL_STATUSES:
        assert is_legal_transition(st, st) is False


def test_is_legal_transition_from_attached_always_rejected():
    """`attached` is terminal -- it has no entry in `_NEXT_STATUS`, so every
    requested status (including re-requesting `attached` itself) must be
    rejected."""
    for requested in ALL_STATUSES:
        assert is_legal_transition(LabOrderStatus.attached, requested) is False


# ---------------------------------------------------------------------------
# validate_result_for_transition
# ---------------------------------------------------------------------------


def test_validate_result_verified_requires_non_empty_result():
    error = validate_result_for_transition(LabOrderStatus.verified, None)
    assert error is not None


def test_validate_result_verified_rejects_empty_string():
    error = validate_result_for_transition(LabOrderStatus.verified, "")
    assert error is not None


def test_validate_result_verified_rejects_whitespace_only():
    error = validate_result_for_transition(LabOrderStatus.verified, "   \t\n  ")
    assert error is not None


def test_validate_result_verified_accepts_non_empty_result():
    error = validate_result_for_transition(LabOrderStatus.verified, "Hemoglobin: 13.5 g/dL")
    assert error is None


@pytest.mark.parametrize(
    "requested",
    [LabOrderStatus.collected, LabOrderStatus.processing, LabOrderStatus.attached],
)
def test_validate_result_non_verify_transitions_reject_a_present_result(requested):
    """Every transition other than the one landing on `verified` must NOT
    receive a `result` -- a client sneaking one in on, say,
    `ordered -> collected` is rejected outright, not silently ignored."""
    error = validate_result_for_transition(requested, "some sneaky value")
    assert error is not None


@pytest.mark.parametrize(
    "requested",
    [LabOrderStatus.collected, LabOrderStatus.processing, LabOrderStatus.attached],
)
def test_validate_result_non_verify_transitions_with_no_result_is_legal(requested):
    """The flip side of the above: no `result` on a non-verify transition is
    the ordinary, legal case and must return `None` (no error)."""
    error = validate_result_for_transition(requested, None)
    assert error is None


def test_validate_result_ordered_with_result_present_is_rejected():
    """`ordered` is never a real `requested` value the API accepts (it's not
    in `TransitionRequest.to_status`'s Literal), but the pure function itself
    makes no such assumption -- it should still apply the "no result allowed"
    rule uniformly to any status other than `verified`."""
    error = validate_result_for_transition(LabOrderStatus.ordered, "value")
    assert error is not None
