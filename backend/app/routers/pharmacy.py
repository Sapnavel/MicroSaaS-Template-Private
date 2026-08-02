"""Pharmacy Inventory module (FEFO batch management) -- replaces the
original stub. Business logic lives in services/pharmacy_service.py (which
itself delegates the actual FEFO allocation/locking to
services/pharmacy_engine.py) -- this module is request validation + response
shaping only, matching the router-thin/service-thick pattern in
routers/lab.py / routers/consultations.py.

Branch tenancy is REAL here (PRPs/pharmacy-module-prp.md "Key design
decisions" #1) -- unlike Patient/Consultation/Lab, which are all
deliberately branch-agnostic. All branch-resolution logic (pharmacist always
scoped to their own branch; system_admin must supply branch_id on writes,
may omit it on reads to mean "all branches") lives in
`pharmacy_service._resolve_branch_id` / `_resolve_branch_filter`, not here --
this router never inspects `current_user.branch_id` or the request's
`branch_id` itself.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.pharmacy import (
    BatchCreate,
    BatchResponse,
    DispenseRequest,
    DispenseResponse,
    ExpiringBatchResponse,
    InventoryItemCreate,
    InventoryItemResponse,
    LowStockItemResponse,
)
from app.services import pharmacy_service
from app.services.pharmacy_engine import InsufficientStockError, InventoryItemNotFound

router = APIRouter(prefix="/api/v1/pharmacy", tags=["pharmacy"])

_PHARMACY_ROLES = ("pharmacist", "system_admin")
_DEFAULT_EXPIRING_WITHIN_DAYS = 30


@router.post("/items", response_model=None)
def create_or_update_item(
    payload: InventoryItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PHARMACY_ROLES)),
) -> JSONResponse:
    """Find-or-update semantics: 201 if this is a newly created
    `InventoryItem` for `(branch_id, drug_id)`, 200 if a row already existed
    and its `reorder_threshold` was updated -- `InventoryItem`'s
    `UNIQUE(branch_id, drug_id)` means this can never be a second insert.
    `response_model=None` + a hand-built `JSONResponse` because the status
    code varies per-call (FastAPI's declarative `status_code=` can't express
    "201 or 200 depending on outcome")."""
    response, created = pharmacy_service.create_or_update_item(db, current_user, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=response.model_dump(mode="json"),
    )


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def receive_batch(
    payload: BatchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PHARMACY_ROLES)),
) -> BatchResponse:
    """Auto-creates the `InventoryItem` (reorder_threshold=0) if this branch
    has never stocked this drug before -- a first-time stock receipt is a
    normal event, not an error."""
    return pharmacy_service.receive_batch(db, current_user, payload)


@router.post("/dispense", response_model=None)
def dispense(
    payload: DispenseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PHARMACY_ROLES)),
) -> DispenseResponse | JSONResponse:
    """The 404/409 bodies below are built by hand, NOT via
    `HTTPException(detail=...)` -- FastAPI always wraps `.detail` as
    `{"detail": ...}`, but the 409 case's API contract requires `available`/
    `requested` as top-level response keys alongside `detail`, matching the
    same body-shape discipline `routers/patients.py`'s `create_patient` /
    `routers/consultations.py`'s `create_prescription` use for their own
    structured error bodies."""
    try:
        return pharmacy_service.dispense(db, current_user, payload)
    except InventoryItemNotFound as exc:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc)},
        )
    except InsufficientStockError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "available": exc.available,
                "requested": exc.requested,
            },
        )


@router.get("/low-stock")
def get_low_stock(
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PHARMACY_ROLES)),
) -> list[LowStockItemResponse]:
    """Branch-scoped from `current_user.branch_id` for a pharmacist (a
    mismatched explicit `branch_id` query param is a 422, same rule as the
    write endpoints); system_admin may pass any `branch_id` or omit it to
    mean "all branches" (see `pharmacy_service._resolve_branch_filter`)."""
    return pharmacy_service.get_low_stock(db, current_user, branch_id)


@router.get("/expiring")
def get_expiring(
    within_days: int = Query(default=_DEFAULT_EXPIRING_WITHIN_DAYS, gt=0),
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PHARMACY_ROLES)),
) -> list[ExpiringBatchResponse]:
    """`within_days` defaults to 30 when omitted. Never includes
    already-expired batches -- this is an upcoming-alert query, not a
    "what's gone bad" report. Same branch-scoping rule as `get_low_stock`."""
    return pharmacy_service.get_expiring(db, current_user, branch_id, within_days)
