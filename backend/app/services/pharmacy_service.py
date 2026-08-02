"""Pharmacy Inventory module business logic. Routers (routers/pharmacy.py)
stay thin and delegate here -- same "business logic lives in services/,
router is thin" split as services/consultation_service.py,
services/patient_service.py, services/lab_service.py.

Design notes (see PRPs/pharmacy-module-prp.md and the Phase 2 brief):

- BRANCH RESOLUTION (the one genuinely new judgment area this module
  introduces): a `pharmacist` caller's branch_id is ALWAYS their own branch
  -- resolved via `core.security.get_caller_branch_id` (the token-claims-
  preferred value `authorize()`'s tenant guard also uses -- see
  `_caller_branch_uuid`, and REVIEW-AGENT finding H1 for why reading
  `current_user.branch_id` directly here was a real, if latent,
  inconsistency with the tenant guard) -- if the request body/query param
  also carries a `branch_id` that does not match,
  this is a 422, never a silent override or a silent accept, same "never
  silently ignore conflicting client input" discipline the Auth module's
  `RegisterRequest`/`register_patient` established for role/branch_id (see
  `services/auth_service.register_patient`'s docstring). `system_admin` has
  no branch of their own: `branch_id` is REQUIRED in the body for the three
  write endpoints (422 if missing), and OPTIONAL for the two GET filters,
  where omitting it is read as "all branches" -- a judgment call, documented
  here: there is no natural "current branch" for an admin account, and
  making a cross-branch operational view (e.g. "show me every branch's
  low-stock items") the natural reading of "no filter given" is more useful
  than erroring or returning nothing. `_resolve_branch_id` (write, always
  returns a concrete UUID) and `_resolve_branch_filter` (read, may return
  `None` for system_admin) are the two shared helpers implementing this --
  every endpoint routes through exactly one of them, never re-implements the
  mismatch check inline.

- TENANT GUARD -- every write/read below also calls `core.security.authorize`
  against a resource (or a lightweight stand-in) carrying the resolved
  `branch_id`, so `authorize()`'s generic ABAC tenant guard actually fires
  for this module (PRPs/pharmacy-module-prp.md's "Key design decisions" #1:
  this is the first module in the build sequence where that guard applies
  for real). In practice `_resolve_branch_id`/`_resolve_branch_filter` above
  already reject any cross-branch attempt before `authorize()` is ever
  reached for a pharmacist caller -- the `authorize()` call is intentional
  defense-in-depth (same "more than one place enforces the invariant" pattern
  the Lab module's `_get_order_or_404_for_update` + `IntegrityError` catch
  uses), not the only thing standing between a pharmacist and another
  branch's stock.

- `dispense` here is a THIN wrapper around `services/pharmacy_engine.dispense`:
  it does not catch `InventoryItemNotFound`/`InsufficientStockError` itself --
  those propagate to `routers/pharmacy.py`, which maps them to 404/409 (see
  that module for the hand-built JSONResponse bodies). Only on the happy path
  does this function call `record_audit_event` and `db.commit()` -- mirrors
  `services/lab_service.transition_order`'s "engine validates, service
  persists+audits+commits" split.

- `receive_batch` auto-creates the `InventoryItem` (reorder_threshold=0) the
  first time a branch stocks a given drug -- a first-time stock receipt is a
  normal event, not an error (PRP "ENDPOINTS" table).
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import authorize, get_caller_branch_id
from app.models.audit import record_audit_event
from app.models.consultation import Drug
from app.models.pharmacy import InventoryBatch, InventoryItem
from app.models.user import User, UserRole
from app.schemas.pharmacy import (
    AllocationResponse,
    BatchCreate,
    BatchResponse,
    DispenseRequest,
    DispenseResponse,
    ExpiringBatchResponse,
    InventoryItemCreate,
    InventoryItemResponse,
    LowStockItemResponse,
)
from app.services import pharmacy_engine

_DEFAULT_EXPIRING_WITHIN_DAYS = 30


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard when there
    is no single ORM row to check against (the two GET list endpoints, when
    scoped to one branch) -- only `branch_id` matters to the guard, so a
    full `InventoryItem` fetch isn't needed just to authorize a query."""

    branch_id: uuid.UUID


# --- Branch resolution ---------------------------------------------------


def _caller_branch_uuid(current_user: User) -> uuid.UUID | None:
    """Resolve the caller's branch via `core.security.get_caller_branch_id`
    (token-claims-preferred), NOT `current_user.branch_id` directly.

    REVIEW-AGENT finding H1 (PRPs/pharmacy-module-prp.md Phase 3): the
    earlier version of this module read `current_user.branch_id` here --
    a live DB value -- while `authorize()`'s tenant guard (called later in
    every one of this module's write/read paths) compares against the
    JWT-frozen branch via `get_caller_branch_id`. If an admin reassigns a
    pharmacist's branch mid-session without forcing re-login, those two
    checks would disagree: this function would resolve writes against the
    NEW branch (fresh DB read), `pharmacy_engine.dispense` would lock and
    decrement that branch's stock in memory, and only then would
    `authorize()` compare the OLD (token) branch and deny -- fragile
    (currently fails closed only because nothing commits before that later
    check, not because the two checks actually agree). Using the same
    helper here closes that gap: both checks now derive from one frozen
    value for the lifetime of the request.
    """
    branch_id_str = get_caller_branch_id(current_user)
    return uuid.UUID(branch_id_str) if branch_id_str else None


def _resolve_branch_id(current_user: User, payload_branch_id: uuid.UUID | None) -> uuid.UUID:
    """Shared branch-resolution helper for all three WRITE endpoints
    (items, batches, dispense) -- see module docstring's "BRANCH RESOLUTION"
    section. Always returns a concrete branch_id or raises 422; never `None`.
    """
    if current_user.role == UserRole.pharmacist:
        caller_branch_id = _caller_branch_uuid(current_user)
        if caller_branch_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "your account has no branch assigned; contact an administrator",
            )
        if payload_branch_id is not None and payload_branch_id != caller_branch_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "branch_id in the request does not match your assigned branch",
            )
        return caller_branch_id

    if current_user.role == UserRole.system_admin:
        if payload_branch_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "branch_id is required for system_admin callers",
            )
        return payload_branch_id

    # Unreachable via the router (require_role gates to pharmacist/system_admin
    # only), kept as a defensive fallback rather than an unchecked assumption.
    raise HTTPException(status.HTTP_403_FORBIDDEN, "role not permitted for this operation")


def _resolve_branch_filter(current_user: User, branch_id_param: uuid.UUID | None) -> uuid.UUID | None:
    """Same mismatch-rejection rule as `_resolve_branch_id`, for the two GET
    endpoints' `branch_id` query-param filtering. Returns `None` only for a
    system_admin caller who omitted `branch_id` -- read as "all branches"
    (see module docstring)."""
    if current_user.role == UserRole.pharmacist:
        caller_branch_id = _caller_branch_uuid(current_user)
        if caller_branch_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "your account has no branch assigned; contact an administrator",
            )
        if branch_id_param is not None and branch_id_param != caller_branch_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "branch_id in the request does not match your assigned branch",
            )
        return caller_branch_id

    if current_user.role == UserRole.system_admin:
        return branch_id_param  # None means "all branches" for this role.

    raise HTTPException(status.HTTP_403_FORBIDDEN, "role not permitted for this operation")


def _get_drug_or_404(db: Session, drug_id: uuid.UUID) -> Drug:
    drug = db.get(Drug, drug_id)
    if drug is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "drug not found")
    return drug


# --- Items -----------------------------------------------------------------


def create_or_update_item(
    db: Session, current_user: User, payload: InventoryItemCreate
) -> tuple[InventoryItemResponse, bool]:
    """POST /api/v1/pharmacy/items. Returns `(response, created)` so the
    router can pick 201 (new row) vs 200 (existing row, threshold updated) --
    `InventoryItem`'s `UNIQUE(branch_id, drug_id)` constraint means this must
    be find-or-update, never a second insert attempt for an existing pair."""
    branch_id = _resolve_branch_id(current_user, payload.branch_id)
    _get_drug_or_404(db, payload.drug_id)

    item = db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == branch_id,
            InventoryItem.drug_id == payload.drug_id,
        )
    ).scalar_one_or_none()

    created = item is None
    if created:
        item = InventoryItem(
            branch_id=branch_id,
            drug_id=payload.drug_id,
            reorder_threshold=payload.reorder_threshold,
        )
        db.add(item)
    else:
        item.reorder_threshold = payload.reorder_threshold
        db.add(item)

    authorize(current_user, "inventory_item", "write", item)

    db.commit()
    db.refresh(item)
    return InventoryItemResponse.model_validate(item), created


# --- Batches -----------------------------------------------------------------


def receive_batch(db: Session, current_user: User, payload: BatchCreate) -> BatchResponse:
    """POST /api/v1/pharmacy/batches. Auto-creates the `InventoryItem`
    (reorder_threshold=0) if this branch has never stocked this drug before
    -- see module docstring. `record_audit_event(action="pharmacy.batch_received", ...)`
    on success (new stock entering the system is as auditable as stock
    leaving it, per the Phase 2 brief)."""
    branch_id = _resolve_branch_id(current_user, payload.branch_id)
    _get_drug_or_404(db, payload.drug_id)

    item = db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == branch_id,
            InventoryItem.drug_id == payload.drug_id,
        )
    ).scalar_one_or_none()
    if item is None:
        item = InventoryItem(branch_id=branch_id, drug_id=payload.drug_id, reorder_threshold=0)
        db.add(item)
        db.flush()  # populate item.id for the batch FK below

    authorize(current_user, "inventory_item", "write", item)

    batch = InventoryBatch(
        inventory_item_id=item.id,
        batch_number=payload.batch_number,
        quantity=payload.quantity,
        expiry_date=payload.expiry_date,
    )
    db.add(batch)
    db.flush()  # populate batch.id/received_at for the audit log + response

    record_audit_event(
        db,
        branch_id=branch_id,
        actor_user_id=current_user.id,
        action="pharmacy.batch_received",
        resource_type="inventory_batch",
        resource_id=str(batch.id),
        metadata={
            "inventory_item_id": str(item.id),
            "drug_id": str(payload.drug_id),
            "batch_number": payload.batch_number,
            "quantity": payload.quantity,
            "expiry_date": payload.expiry_date.isoformat(),
        },
    )

    db.commit()
    db.refresh(batch)
    return BatchResponse.model_validate(batch)


# --- Dispense ----------------------------------------------------------------


def dispense(db: Session, current_user: User, payload: DispenseRequest) -> DispenseResponse:
    """POST /api/v1/pharmacy/dispense. Thin wrapper around
    `pharmacy_engine.dispense` -- catches nothing itself. If the engine raises
    `InventoryItemNotFound`/`InsufficientStockError`, this function exits via
    that exception (no audit call, no commit reached) and the router maps it
    to 404/409. Only the happy path below runs `record_audit_event` +
    `db.commit()`, in the SAME transaction the engine already decremented
    batch quantities in (mirrors `lab_service.transition_order`'s "engine
    validates, service persists+audits+commits" flow)."""
    branch_id = _resolve_branch_id(current_user, payload.branch_id)

    result = pharmacy_engine.dispense(
        db,
        branch_id=branch_id,
        drug_id=payload.drug_id,
        quantity=payload.quantity,
        actor_user_id=current_user.id,
        reference=payload.reference,
    )

    authorize(current_user, "inventory_item", "write", result.inventory_item)

    record_audit_event(
        db,
        branch_id=branch_id,
        actor_user_id=current_user.id,
        action="pharmacy.dispensed",
        resource_type="inventory_item",
        resource_id=str(result.inventory_item.id),
        metadata={
            "drug_id": str(result.drug_id),
            "branch_id": str(result.branch_id),
            "quantity": result.quantity_dispensed,
            "reference": result.reference,
            "allocations": [
                {
                    "batch_id": str(a.batch_id),
                    "batch_number": a.batch_number,
                    "expiry_date": a.expiry_date.isoformat(),
                    "quantity_taken": a.quantity_taken,
                }
                for a in result.allocations
            ],
        },
    )

    db.commit()

    return DispenseResponse(
        inventory_item_id=result.inventory_item.id,
        branch_id=result.branch_id,
        drug_id=result.drug_id,
        quantity_dispensed=result.quantity_dispensed,
        allocations=[
            AllocationResponse(
                batch_id=a.batch_id,
                batch_number=a.batch_number,
                expiry_date=a.expiry_date,
                quantity_taken=a.quantity_taken,
            )
            for a in result.allocations
        ],
    )


# --- Alerts ------------------------------------------------------------------


def get_low_stock(
    db: Session, current_user: User, branch_id_param: uuid.UUID | None
) -> list[LowStockItemResponse]:
    """GET /api/v1/pharmacy/low-stock. Items where
    `SUM(inventory_batches.quantity WHERE expiry_date >= today)` is less than
    `reorder_threshold` -- a single joined/aggregated query (one query, not
    one `Drug` lookup per item, to avoid N+1)."""
    branch_id = _resolve_branch_filter(current_user, branch_id_param)
    if branch_id is not None:
        authorize(current_user, "inventory_item", "read", _BranchScoped(branch_id=branch_id))
    # else: system_admin querying across every branch -- no single resource
    # to authorize against; system_admin already bypasses the tenant guard
    # entirely inside authorize() (core/security.py's _admin_bypass), and no
    # other role can reach this branch (pharmacist always resolves to a
    # concrete branch_id above).

    today = date.today()
    current_qty_subq = (
        select(
            InventoryBatch.inventory_item_id.label("inventory_item_id"),
            func.sum(InventoryBatch.quantity).label("current_quantity"),
        )
        .where(InventoryBatch.expiry_date >= today)
        .group_by(InventoryBatch.inventory_item_id)
        .subquery()
    )

    current_quantity_expr = func.coalesce(current_qty_subq.c.current_quantity, 0)

    query = (
        select(
            InventoryItem.id,
            InventoryItem.branch_id,
            InventoryItem.drug_id,
            Drug.name,
            current_quantity_expr,
            InventoryItem.reorder_threshold,
        )
        .join(Drug, Drug.id == InventoryItem.drug_id)
        .outerjoin(current_qty_subq, current_qty_subq.c.inventory_item_id == InventoryItem.id)
        .where(current_quantity_expr < InventoryItem.reorder_threshold)
    )
    if branch_id is not None:
        query = query.where(InventoryItem.branch_id == branch_id)

    rows = db.execute(query).all()
    return [
        LowStockItemResponse(
            inventory_item_id=row[0],
            branch_id=row[1],
            drug_id=row[2],
            drug_name=row[3],
            current_quantity=row[4],
            reorder_threshold=row[5],
        )
        for row in rows
    ]


def get_expiring(
    db: Session,
    current_user: User,
    branch_id_param: uuid.UUID | None,
    within_days: int,
) -> list[ExpiringBatchResponse]:
    """GET /api/v1/pharmacy/expiring?within_days=N. Batches where
    `today <= expiry_date <= today + within_days days` -- already-expired
    batches (`expiry_date < today`) are NEVER included, this is an
    upcoming-alert query, not a "what's gone bad" report (same expired-batch
    exclusion discipline `pharmacy_engine.dispense` uses). Same
    join-for-drug_name N+1 avoidance as `get_low_stock`."""
    branch_id = _resolve_branch_filter(current_user, branch_id_param)
    if branch_id is not None:
        authorize(current_user, "inventory_item", "read", _BranchScoped(branch_id=branch_id))

    today = date.today()
    horizon = today + timedelta(days=within_days)

    query = (
        select(
            InventoryBatch.id,
            InventoryBatch.inventory_item_id,
            InventoryItem.drug_id,
            Drug.name,
            InventoryBatch.batch_number,
            InventoryBatch.quantity,
            InventoryBatch.expiry_date,
        )
        .join(InventoryItem, InventoryItem.id == InventoryBatch.inventory_item_id)
        .join(Drug, Drug.id == InventoryItem.drug_id)
        .where(InventoryBatch.expiry_date >= today, InventoryBatch.expiry_date <= horizon)
    )
    if branch_id is not None:
        query = query.where(InventoryItem.branch_id == branch_id)

    rows = db.execute(query).all()
    return [
        ExpiringBatchResponse(
            batch_id=row[0],
            inventory_item_id=row[1],
            drug_id=row[2],
            drug_name=row[3],
            batch_number=row[4],
            quantity=row[5],
            expiry_date=row[6],
        )
        for row in rows
    ]
