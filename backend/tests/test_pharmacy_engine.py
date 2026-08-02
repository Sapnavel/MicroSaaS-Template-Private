"""Unit tests for the pure FEFO allocation function
`app.services.pharmacy_engine.allocate_fefo`, per
PRPs/pharmacy-module-prp.md's Validation Gate 1 ("the FEFO selection/
allocation logic ... is unit-testable as a pure function separate from the
DB-locking wrapper").

No DB, no fixtures from conftest.py -- these are plain Python list-in,
dataclass-out tests. `candidates` is a list of `(batch_id, available_quantity)`
tuples already assumed to be in FEFO order (earliest expiry first); this
function trusts that order and never re-sorts, so tests below construct
candidate lists already in the order the caller would have queried them.
"""

import uuid

import pytest

from app.services.pharmacy_engine import allocate_fefo


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_single_batch_covers_request_exactly():
    b1, = _ids(1)
    result = allocate_fefo(10, [(b1, 10)])

    assert result.fully_satisfied is True
    assert result.total_available == 10
    assert result.allocations == [(b1, 10)]


def test_single_batch_has_more_than_requested():
    """Partial pull from a single (sufficient) batch -- the batch itself is
    not exhausted, only `requested_quantity` is taken from it."""
    b1, = _ids(1)
    result = allocate_fefo(4, [(b1, 10)])

    assert result.fully_satisfied is True
    assert result.total_available == 10
    assert result.allocations == [(b1, 4)]


def test_request_spans_two_batches_with_partial_pull_from_the_second():
    """First batch (earliest expiry) is fully consumed, second batch covers
    the remainder with quantity left over -- confirms `allocate_fefo` stops
    as soon as the request is satisfied rather than continuing to drain
    later batches."""
    b1, b2 = _ids(2)
    # b1 has 5, request is 12 -> b1 fully consumed (5), b2 partially (7 of 20).
    result = allocate_fefo(12, [(b1, 5), (b2, 20)])

    assert result.fully_satisfied is True
    assert result.total_available == 25
    assert result.allocations == [(b1, 5), (b2, 7)]


def test_request_spans_three_candidates_partial_on_last_needed_batch_untouched_third():
    """Mirrors the integration test's 3-batch dispense shape: request pulls
    from the first two batches (fully draining the first, partially the
    second) and never touches the third (latest-expiry) candidate at all --
    a batch not mentioned in `allocations` is implicitly untouched."""
    b1, b2, b3 = _ids(3)
    result = allocate_fefo(15, [(b1, 10), (b2, 20), (b3, 100)])

    assert result.fully_satisfied is True
    assert result.total_available == 130
    # b1 fully drained (10), b2 partially (5 of 20), b3 never appears.
    assert result.allocations == [(b1, 10), (b2, 5)]
    allocated_batch_ids = {batch_id for batch_id, _ in result.allocations}
    assert b3 not in allocated_batch_ids


def test_request_exceeds_total_available_returns_empty_allocations():
    """All-or-nothing: if the sum across every candidate is less than the
    request, `fully_satisfied` is False and `allocations` is empty -- never
    a partial allocation the caller could accidentally apply."""
    b1, b2 = _ids(2)
    result = allocate_fefo(100, [(b1, 10), (b2, 20)])

    assert result.fully_satisfied is False
    assert result.allocations == []
    assert result.total_available == 30


def test_request_exactly_equals_total_available():
    b1, b2, b3 = _ids(3)
    result = allocate_fefo(30, [(b1, 10), (b2, 15), (b3, 5)])

    assert result.fully_satisfied is True
    assert result.total_available == 30
    assert result.allocations == [(b1, 10), (b2, 15), (b3, 5)]
    assert sum(qty for _, qty in result.allocations) == 30


def test_zero_requested_quantity_raises_value_error():
    b1, = _ids(1)
    with pytest.raises(ValueError):
        allocate_fefo(0, [(b1, 10)])


def test_negative_requested_quantity_raises_value_error():
    b1, = _ids(1)
    with pytest.raises(ValueError):
        allocate_fefo(-5, [(b1, 10)])


def test_empty_candidates_list_is_insufficient_stock_not_an_error():
    """No batches at all is a normal (if unhelpful) insufficient-stock
    outcome, not a `ValueError` -- only a non-positive `requested_quantity`
    is a caller bug."""
    result = allocate_fefo(1, [])

    assert result.fully_satisfied is False
    assert result.allocations == []
    assert result.total_available == 0


def test_zero_quantity_batches_never_appear_in_allocations():
    """A candidate with 0 available contributes nothing and must never show
    up as a `(batch_id, 0)` entry, even though it's a valid input to this
    function (candidate filtering by `quantity > 0` is the DB query's job,
    not this function's -- but a defensive check here costs nothing)."""
    b1, b2 = _ids(2)
    result = allocate_fefo(5, [(b1, 0), (b2, 5)])

    assert result.fully_satisfied is True
    assert result.allocations == [(b2, 5)]
    assert all(qty > 0 for _, qty in result.allocations)
