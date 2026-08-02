"""FEFO (First Expired, First Out) dispense engine for the Pharmacy
Inventory module (PRPs/pharmacy-module-prp.md, "FEFO DISPENSE ENGINE
DESIGN"). Mirrors the rigor of `services/scheduling_engine.py` (consistent
lock ordering to avoid deadlock, explicit exception types, all-or-nothing
transactional semantics) and applies the concurrency-safety lesson the Lab
module's REVIEW-AGENT had to add *after the fact* (Phase 3, finding H1):
`services/lab_service.py`'s `_get_order_or_404_for_update` locks a row with
`SELECT ... FOR UPDATE` before any decision is made from its current state,
specifically to close a TOCTOU race where two concurrent requests both read
stale state before either commits. This module is built with that lock from
the start (`dispense`'s batch query below) rather than needing a Phase 3 fix.

Three design decisions this module makes explicit, because each is a real
judgment call, not an incidental implementation detail:

(a) WHY EXPIRED BATCHES ARE EXCLUDED ENTIRELY, NOT JUST DEPRIORITIZED --
    "First Expired, First Out" could be misread as "dispense whatever's
    oldest, including stock that has already expired." That reading is
    wrong: dispensing expired medication is a patient-safety violation, not
    a data-modeling nuance to optimize around. `dispense`'s batch query
    filters `expiry_date >= as_of` at the SQL level, so an expired batch is
    never a FEFO candidate, never locked, never appears in an allocation,
    and never appears in the result -- it is invisible to this function the
    same way a cancelled appointment is invisible to
    `scheduling_engine._overlap_exists`. Writing off expired stock is a
    separate, out-of-scope concern (a future dedicated endpoint), not
    something this engine's allocation logic should ever touch, even
    read-only.

(b) WHY DISPENSE IS ALL-OR-NOTHING -- if the sum of every non-expired,
    in-stock batch for a drug is less than the requested quantity, the
    entire request fails with `InsufficientStockError` and NOTHING is
    decremented. A partial fill (e.g. handing over half a course of
    antibiotics because stock ran out mid-transaction) is a worse failure
    mode than a clean rejection: a clean 409 gives the pharmacist a
    real signal ("order more stock," "substitute a drug") a silent partial
    dispense would not. This is why `dispense` sums the locked candidates'
    quantities in Python and compares against the request BEFORE applying
    any decrement, rather than decrementing greedily and rolling back on
    shortfall -- summing first means there is never a decremented value
    that needs undoing, which is simpler to reason about and impossible to
    get wrong via a forgotten rollback path.

(c) THE "ENGINE COMPUTES, SERVICE LAYER PERSISTS" BOUNDARY -- `dispense`
    below queries, locks, sums, allocates, and applies in-memory quantity
    decrements to already-loaded ORM objects, but it deliberately does NOT
    call `db.commit()` or `record_audit_event`. That is Phase 2's
    `services/pharmacy_service.py` responsibility, exactly the same split
    `services/lab_workflow.py` keeps from `services/lab_service.py`, and
    `services/prescription_safety.py` keeps from
    `services/consultation_service.py`: the hard, safety-critical logic
    (what to allocate, in what order, whether the request can be satisfied
    at all) lives in a module that never touches a live transaction commit,
    which is what makes `allocate_fefo` below unit-testable with a plain
    Python list and no DB fixture (this PRP's Validation Gate 1 calls this
    out explicitly). Keeping the commit boundary in the service layer also
    means Phase 2 controls exactly when the audit log entry is written
    relative to the commit, without this module needing to know anything
    about audit logging at all.

Lock ordering: candidate batches are locked in one query, in a single
`ORDER BY expiry_date ASC, id ASC` pass (see `dispense` below) --
`expiry_date ASC` is the FEFO ordering itself; `id ASC` is a deterministic
tiebreaker for batches sharing an expiry date. Locking ALL candidates in one
statement, in this fixed order, is the same "acquire resources in a globally
consistent order" discipline `scheduling_engine.py` uses for its Redis lock
keys (sorted so two concurrent bookings targeting overlapping resources
always request them in the same order and therefore serialize instead of
deadlocking) -- applied here with Postgres row locks instead of a
distributed lock, since a single `SELECT ... FOR UPDATE` already gives us
one consistent acquisition order for free.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pharmacy import InventoryBatch, InventoryItem


class InventoryItemNotFound(Exception):
    """Raised when no `InventoryItem` row exists for `(branch_id, drug_id)`
    at all -- this branch has never stocked this drug (or it was stocked
    under a different branch). Phase 2's router turns this into a 404, per
    the PRP's endpoint table."""

    def __init__(self, *, branch_id: uuid.UUID, drug_id: uuid.UUID) -> None:
        self.branch_id = branch_id
        self.drug_id = drug_id
        super().__init__(f"no inventory item exists for branch_id={branch_id}, drug_id={drug_id}")


class InsufficientStockError(Exception):
    """Raised when the sum of every non-expired, in-stock batch for the
    requested drug is less than the requested quantity. Carries both
    `available` (the actual sum, computed by locking every candidate first --
    see module docstring point (b)) and `requested`, so the caller (Phase
    2's router, which turns this into a 409) can report the real shortfall
    to the pharmacist instead of a bare "no."

    Nothing has been decremented and no commit has happened by the time this
    is raised -- `dispense` raises this before touching any batch's
    `quantity`.
    """

    def __init__(self, *, available: int, requested: int) -> None:
        self.available = available
        self.requested = requested
        super().__init__(f"insufficient stock: requested {requested}, only {available} available")


@dataclass(frozen=True)
class FEFOAllocationResult:
    """Result of the pure allocation function `allocate_fefo`.

    `allocations` is the FEFO breakdown -- a list of `(batch_id,
    quantity_taken)` pairs, in the same order the candidates were given
    (earliest expiry first), with a zero-quantity batch never appearing (no
    `(batch_id, 0)` entries). It is empty whenever `fully_satisfied` is
    `False`: this function never returns a partial allocation for the caller
    to apply -- see module docstring point (b), all-or-nothing is enforced
    here at the allocation-decision level, not just at the DB-write level,
    so there is exactly one place in the codebase that decides "is this
    dispense satisfiable," and callers can never accidentally apply a
    partial result.

    `total_available` is the sum of every candidate's quantity, regardless
    of whether the request was satisfied -- this is what
    `InsufficientStockError.available` is built from.
    """

    allocations: list[tuple[uuid.UUID, int]]
    fully_satisfied: bool
    total_available: int


def allocate_fefo(
    requested_quantity: int, candidates: Sequence[tuple[uuid.UUID, int]]
) -> FEFOAllocationResult:
    """Pure allocation function: no DB access, no ORM objects, no session --
    just plain Python values in and out. This is what this PRP's Validation
    Gate 1 means by "unit-testable as a pure function separate from the
    DB-locking wrapper": a caller (`dispense` below, or a unit test) hands
    this a plain list of `(batch_id, available_quantity)` tuples already
    sorted in FEFO order (earliest expiry first -- sorting/filtering by
    expiry_date and excluding expired/exhausted batches is the DB query's
    job in `dispense`, NOT this function's; this function trusts the order
    and contents it is given and does not re-sort or re-filter).

    Walks `candidates` in the given order, taking `min(available, still
    needed)` from each until the request is fully covered. Returns a
    `FEFOAllocationResult`:
      - If `sum(available for _, available in candidates) < requested_quantity`:
        `fully_satisfied=False`, `allocations=[]` (nothing is ever partially
        allocated -- see `FEFOAllocationResult`'s docstring), `total_available`
        set to the actual sum so the caller can report the shortfall.
      - Otherwise: `fully_satisfied=True`, `allocations` holds the per-batch
        breakdown that sums to exactly `requested_quantity`, `total_available`
        set to the sum (redundant with success but kept for a uniform return
        shape regardless of outcome).

    Raises `ValueError` if `requested_quantity` is not positive -- a
    zero/negative dispense request is a caller bug, not a stock-availability
    question this function should try to answer.
    """
    if requested_quantity <= 0:
        raise ValueError(f"requested_quantity must be positive, got {requested_quantity}")

    total_available = sum(available for _, available in candidates)

    if total_available < requested_quantity:
        return FEFOAllocationResult(allocations=[], fully_satisfied=False, total_available=total_available)

    remaining = requested_quantity
    allocations: list[tuple[uuid.UUID, int]] = []
    for batch_id, available in candidates:
        if remaining <= 0:
            break
        take = min(available, remaining)
        if take > 0:
            allocations.append((batch_id, take))
            remaining -= take

    return FEFOAllocationResult(allocations=allocations, fully_satisfied=True, total_available=total_available)


@dataclass
class BatchAllocationDetail:
    """One line of a dispense's per-batch breakdown -- enough for Phase 2 to
    both shape an API response line item and populate the audit log's
    `metadata` without re-querying `InventoryBatch` rows that (post-dispense)
    may already have hit zero quantity."""

    batch_id: uuid.UUID
    batch_number: str
    expiry_date: date
    quantity_taken: int


@dataclass
class DispenseResult:
    """Everything Phase 2's `pharmacy_service.dispense` (a thin wrapper
    around this module's `dispense`) needs to commit, shape a response, and
    call `record_audit_event` -- without needing to re-derive any of it.

    Judgment call (flagged per the task brief, since Phase 2 depends on this
    shape exactly): this bundles the loaded `inventory_item` ORM object
    (still attached to `db`, not yet committed) rather than just its id, so
    Phase 2 can read `branch_id`/`drug_id` off of it directly if convenient;
    `branch_id`/`drug_id`/`actor_user_id`/`reference` are ALSO included as
    plain fields (not just reachable via `inventory_item`) so Phase 2's
    audit-log call site does not need to touch the ORM object at all if it
    doesn't want to -- both paths work, whichever reads more naturally at
    the call site.
    """

    inventory_item: InventoryItem
    allocations: list[BatchAllocationDetail]
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    quantity_dispensed: int
    actor_user_id: uuid.UUID
    reference: str | None = None


def dispense(
    db: Session,
    *,
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    quantity: int,
    actor_user_id: uuid.UUID,
    reference: str | None = None,
    as_of: date | None = None,
) -> DispenseResult:
    """Resolve `(branch_id, drug_id)` to an `InventoryItem`, lock every
    non-expired in-stock `InventoryBatch` candidate, and allocate `quantity`
    across them in FEFO order -- atomically, all-or-nothing.

    `as_of` is the injectable "today" for the expiry comparison (defaults to
    `date.today()`), the same seam `services/prescription_safety.py`'s
    `as_of` parameter and the Lab/Consultation modules' engines expose, so
    Phase 3's tests can pin a specific date rather than depending on
    wall-clock time.

    Steps (mirrors the PRP's "FEFO DISPENSE ENGINE DESIGN" numbering):
      1. Resolve the `InventoryItem` for `(branch_id, drug_id)`. Raises
         `InventoryItemNotFound` if none exists at all -- this branch has
         never stocked this drug.
      2. Query candidate batches (`quantity > 0 AND expiry_date >= as_of`,
         `ORDER BY expiry_date ASC, id ASC`) with `.with_for_update()` --
         locking every candidate in this ONE query, up front, before any
         allocation decision is made. This is the critical fix that avoids
         the Lab module's original TOCTOU mistake (see module docstring):
         two concurrent `dispense` calls against overlapping batches will
         have the second block on this query until the first's transaction
         commits or rolls back, then re-read the now-current quantities,
         rather than both reading stale quantities and racing to decrement.
      3. Delegate the actual allocation decision to the pure `allocate_fefo`
         function -- this module's DB-facing code does no allocation math
         itself, it only fetches/locks and then applies whatever
         `allocate_fefo` decided.
      4. If `allocate_fefo` reports `fully_satisfied=False`: raise
         `InsufficientStockError(available=..., requested=...)`. Nothing has
         been decremented and nothing has been committed -- this function
         never calls `db.commit()` at all (see module docstring point (c)).
      5. Otherwise: apply each allocation's decrement to its (already
         locked, already loaded) `InventoryBatch` ORM object and return a
         `DispenseResult`. Committing and audit-logging are explicitly NOT
         done here -- that is `services/pharmacy_service.py`'s job (Phase
         2), so this function can be unit-... well, integration-tested
         against a real Postgres transaction and then rolled back with no
         durable side effect, and so a caller can compose additional
         same-transaction work (e.g. decrementing a prescription's
         remaining-refills counter, if a future module adds one) before
         deciding to commit.

    Raises:
        InventoryItemNotFound: no `InventoryItem` for `(branch_id, drug_id)`.
        InsufficientStockError: total available (non-expired, in-stock)
            quantity is less than `quantity` requested.
        ValueError: `quantity` is not positive (see `allocate_fefo`).
    """
    as_of = as_of if as_of is not None else date.today()

    item = db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == branch_id,
            InventoryItem.drug_id == drug_id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise InventoryItemNotFound(branch_id=branch_id, drug_id=drug_id)

    # Lock every FEFO candidate up front, in one statement, in the fixed
    # `expiry_date ASC, id ASC` order -- see module docstring's "Lock
    # ordering" section. Expired batches (`expiry_date < as_of`) are
    # excluded by the WHERE clause itself: they are never locked, never
    # considered, never appear anywhere below (module docstring point (a)).
    candidate_batches = list(
        db.execute(
            select(InventoryBatch)
            .where(
                InventoryBatch.inventory_item_id == item.id,
                InventoryBatch.quantity > 0,
                InventoryBatch.expiry_date >= as_of,
            )
            .order_by(InventoryBatch.expiry_date.asc(), InventoryBatch.id.asc())
            .with_for_update()
        )
        .scalars()
        .all()
    )

    allocation_result = allocate_fefo(
        quantity, [(batch.id, batch.quantity) for batch in candidate_batches]
    )
    if not allocation_result.fully_satisfied:
        raise InsufficientStockError(available=allocation_result.total_available, requested=quantity)

    batches_by_id = {batch.id: batch for batch in candidate_batches}
    details: list[BatchAllocationDetail] = []
    for batch_id, quantity_taken in allocation_result.allocations:
        batch = batches_by_id[batch_id]
        batch.quantity -= quantity_taken
        db.add(batch)
        details.append(
            BatchAllocationDetail(
                batch_id=batch.id,
                batch_number=batch.batch_number,
                expiry_date=batch.expiry_date,
                quantity_taken=quantity_taken,
            )
        )

    return DispenseResult(
        inventory_item=item,
        allocations=details,
        branch_id=branch_id,
        drug_id=drug_id,
        quantity_dispensed=quantity,
        actor_user_id=actor_user_id,
        reference=reference,
    )
