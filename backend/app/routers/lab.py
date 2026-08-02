"""Lab module: order lifecycle tracking (ordered -> collected -> processing
-> verified -> attached). Business logic lives in services/lab_service.py --
this module is request validation + response shaping only, matching the
router-thin/service-thick pattern in routers/consultations.py.

This replaces the original stub, whose own TODO said "PATCH
/api/v1/lab/samples/{id}/status" -- that reads as if `lab_samples` had a
status of its own. It doesn't: the lifecycle status lives on `LabOrder.status`
(see models/lab.py's module docstring), so the real endpoint is
`PATCH /api/v1/lab/orders/{id}/transition`, per PRPs/lab-module-prp.md.

Role gating is two-layered:
- `require_role(...)` on each endpoint below gates *which roles can reach
  this endpoint at all*. Notably, `doctor` is excluded entirely from the
  transition endpoint -- doctors order and read lab results, they never
  transition anything (per the PRP).
- The transition endpoint's *specific* to_status-dependent role requirement
  (e.g. `collected` allows nurse+lab_tech+system_admin, but `processing`/
  `verified`/`attached` require lab_tech+system_admin) is enforced inside
  `lab_service.transition_order`, not here -- one endpoint here serves four
  different transitions with different role requirements, which a single
  router-level `require_role(...)` can't express.

Doctor ownership (a lab order's "owner" is its consultation's doctor) is a
manual check inside `lab_service.py`, not a registered ABAC policy -- same
precedent `consultation_service.start_consultation` set for this exact shape
of problem, per the Phase 2 brief. `require_role` here only gates role
membership; doctor-vs-doctor ownership is enforced in the service layer.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.lab import LabOrderStatus
from app.models.user import User
from app.schemas.lab import LabOrderCreate, LabOrderResponse, LabOrderResponseNoResult, TransitionRequest
from app.services import lab_service

router = APIRouter(prefix="/api/v1/lab", tags=["lab"])

_DOCTOR_ADMIN = ("doctor", "system_admin")
_ALL_LAB_ROLES = ("doctor", "nurse", "lab_tech", "system_admin")
# doctor is deliberately excluded here -- see module docstring.
_TRANSITION_CALLER_ROLES = ("nurse", "lab_tech", "system_admin")


@router.post("/orders", status_code=status.HTTP_201_CREATED)
def create_order(
    payload: LabOrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_ADMIN)),
) -> LabOrderResponse | LabOrderResponseNoResult:
    """Order a lab test against a consultation. `patient_id`/`ordered_by`
    are derived server-side (see schemas.lab.LabOrderCreate), never from
    client input."""
    order = lab_service.create_order(db, current_user, payload)
    return lab_service.shape_lab_order_response(db, current_user, order)


@router.get("/orders/{order_id}")
def get_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ALL_LAB_ROLES)),
) -> LabOrderResponse | LabOrderResponseNoResult:
    order = lab_service.get_order(db, current_user, order_id)
    return lab_service.shape_lab_order_response(db, current_user, order)


@router.get("/orders")
def list_orders(
    patient_id: uuid.UUID | None = None,
    consultation_id: uuid.UUID | None = None,
    status_filter: LabOrderStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ALL_LAB_ROLES)),
) -> list[LabOrderResponse | LabOrderResponseNoResult]:
    """At least one of `patient_id`/`consultation_id`/`status` is required --
    same "no unbounded scan" rule as the Patient module's search endpoint
    (routers/patients.py's search_patients). doctor callers are additionally
    restricted (in lab_service.list_orders) to their own consultations'
    orders, even if patient_id/consultation_id would otherwise match someone
    else's."""
    if patient_id is None and consultation_id is None and status_filter is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "at least one of patient_id, consultation_id, status is required",
        )
    orders = lab_service.list_orders(
        db,
        current_user,
        patient_id=patient_id,
        consultation_id=consultation_id,
        status_filter=status_filter,
    )
    return lab_service.shape_lab_order_responses(db, current_user, orders)


@router.patch("/orders/{order_id}/transition")
def transition_order(
    order_id: uuid.UUID,
    payload: TransitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_TRANSITION_CALLER_ROLES)),
) -> LabOrderResponse | LabOrderResponseNoResult:
    """One endpoint serves all four transitions
    (collected/processing/verified/attached) -- the specific role
    requirement per to_status, plus the state-machine/result validation, are
    all enforced inside lab_service.transition_order (see its docstring for
    the exact validation order and status codes)."""
    order = lab_service.transition_order(db, current_user, order_id, payload)
    return lab_service.shape_lab_order_response(db, current_user, order)
