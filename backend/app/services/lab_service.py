"""Lab module business logic. Routers (routers/lab.py) stay thin and
delegate here -- same "business logic lives in services/, router is thin"
split as services/consultation_service.py and services/patient_service.py.

Design notes (see PRPs/lab-module-prp.md, "ENDPOINTS" / "STATE MACHINE
DESIGN"):

- A lab order's "owner" is its consultation's doctor. This module does a
  MANUAL ownership check (`_authorize_doctor_ownership`), comparing
  `consultation.doctor_id == current_user.doctor_id`, rather than going
  through the generic `authorize()` ABAC registry -- this is the SAME
  precedent `consultation_service.start_consultation` set for exactly this
  shape of problem (no clean existing resource_type to register a policy
  against), per the Phase 2 brief. No new ABAC policy is registered in
  core/security.py for `lab_order`.
- `current_user.doctor_id` (a `doctors.id`, stashed by `get_current_user`)
  is what identifies "which Doctor record is this caller" -- comparing
  against `current_user.id` would compare two different id spaces (same
  gotcha as the Consultation module).
- nurse/lab_tech have no ownership concept for lab orders -- they're gated
  by role alone (`require_role` at the router, plus the per-transition role
  table below for the transition endpoint specifically).
- All state-machine validation (is this transition legal? is this `result`
  legal for this transition?) is delegated to `services/lab_workflow.py`,
  which is pure/DB-independent. This module is what actually calls into it,
  does the DB writes (create/update `LabSample`, stamp actor+timestamp
  columns, flip `order.status`), and audit-logs every successful transition.
- PHI handling: `result` (the decrypted lab value) must never reach a
  `logger.*` call anywhere in this module. The audit log's metadata records
  only whether a result was submitted at a given transition
  (`{"result_submitted": true/false}`), never the value itself -- storing
  the actual PHI value in two places (the `lab_samples` row AND the audit
  log's JSONB metadata) is unnecessary duplication of PHI storage, per the
  PRP's explicit note on this tradeoff.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.events import event_publisher
from app.models.audit import record_audit_event
from app.models.consultation import Consultation
from app.models.lab import LabOrder, LabOrderStatus, LabSample
from app.models.user import User, UserRole
from app.schemas.lab import (
    LabOrderCreate,
    LabOrderResponse,
    LabOrderResponseNoResult,
    LabSampleResponse,
    LabSampleResponseNoResult,
    TransitionRequest,
)
from app.services import lab_workflow

# Per-transition role requirements (PRP "ENDPOINTS" -- "Per-transition
# roles"). Enforced HERE, inside the service function handling the
# transition, not just via a blanket `require_role` at the router, since one
# endpoint (`PATCH /api/v1/lab/orders/{id}/transition`) serves four
# different transitions with different role requirements. `doctor` never
# appears in any of these tuples -- doctors are excluded from the transition
# endpoint entirely at the router level (they order and read, that's it).
_TRANSITION_ROLES: dict[LabOrderStatus, tuple[UserRole, ...]] = {
    LabOrderStatus.collected: (UserRole.nurse, UserRole.lab_tech, UserRole.system_admin),
    LabOrderStatus.processing: (UserRole.lab_tech, UserRole.system_admin),
    LabOrderStatus.verified: (UserRole.lab_tech, UserRole.system_admin),
    LabOrderStatus.attached: (UserRole.lab_tech, UserRole.system_admin),
}


# --- Internal helpers --------------------------------------------------------


def _get_order_or_404(db: Session, order_id: uuid.UUID) -> LabOrder:
    order = db.get(LabOrder, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lab order not found")
    return order


def _get_order_or_404_for_update(db: Session, order_id: uuid.UUID) -> LabOrder:
    """Same as `_get_order_or_404` but takes a row lock (`SELECT ... FOR
    UPDATE`) for the duration of the enclosing transaction. Used only by
    `transition_order` -- REVIEW-AGENT finding H1 (PRPs/lab-module-prp.md
    Phase 3): without this lock, two near-simultaneous transition requests
    for the SAME order (e.g. two nurses both doing `ordered -> collected`)
    both read `order.status`/`_get_sample_for_order` before either commits,
    both pass validation, and both insert -- leaving two `LabSample` rows
    for one order (now also blocked by the DB `UNIQUE` constraint added for
    the same finding, but the lock is what turns that into a clean 409
    instead of an `IntegrityError` surfacing as a 500). The second
    concurrent request now blocks here until the first transaction commits
    or rolls back, then re-reads the (now-updated) status and correctly
    hits `is_legal_transition`'s 409 rather than racing to create a
    duplicate sample or silently lose an update (the same race pattern also
    covers `collected/processing/verified -> next`, not just `collected`)."""
    order = db.execute(select(LabOrder).where(LabOrder.id == order_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lab order not found")
    return order


def _get_consultation_or_404(db: Session, consultation_id: uuid.UUID) -> Consultation:
    consultation = db.get(Consultation, consultation_id)
    if consultation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "consultation not found")
    return consultation


def _get_sample_for_order(db: Session, order_id: uuid.UUID) -> LabSample | None:
    return db.execute(select(LabSample).where(LabSample.lab_order_id == order_id)).scalar_one_or_none()


def _authorize_doctor_ownership(db: Session, current_user: User, consultation_id: uuid.UUID) -> None:
    """Manual ownership check (see module docstring) -- only fires for
    role=doctor. system_admin bypasses; nurse/lab_tech have no ownership
    concept here at all (they never reach this function on a path that
    matters, but it's a no-op for them regardless, for safety)."""
    if current_user.role != UserRole.doctor:
        return
    if current_user.doctor_id is None:
        # Same data-consistency case `consultation_service.start_consultation`
        # calls out explicitly: role=doctor but no matching Doctor row.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "your account has role=doctor but no matching Doctor record; contact an administrator",
        )
    consultation = db.get(Consultation, consultation_id)
    if consultation is None or consultation.doctor_id != current_user.doctor_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "doctors may only access lab orders for their own consultations",
        )


# --- Orders ------------------------------------------------------------------


def create_order(db: Session, current_user: User, payload: LabOrderCreate) -> LabOrder:
    """POST /api/v1/lab/orders. `patient_id` is derived from the referenced
    consultation, `ordered_by` from `current_user.id` -- never from client
    input (see schemas.lab.LabOrderCreate)."""
    consultation = _get_consultation_or_404(db, payload.consultation_id)
    _authorize_doctor_ownership(db, current_user, consultation.id)
    # system_admin bypasses the ownership check inside
    # _authorize_doctor_ownership (it only fires for role=doctor).

    order = LabOrder(
        consultation_id=consultation.id,
        patient_id=consultation.patient_id,
        test_code=payload.test_code,
        ordered_by=current_user.id,
        status=LabOrderStatus.ordered,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, current_user: User, order_id: uuid.UUID) -> LabOrder:
    """GET /api/v1/lab/orders/{id}. doctor callers are restricted to orders
    whose consultation is theirs; nurse/lab_tech/system_admin are
    unrestricted (role-only gating, enforced at the router via
    `require_role`)."""
    order = _get_order_or_404(db, order_id)
    _authorize_doctor_ownership(db, current_user, order.consultation_id)
    return order


def list_orders(
    db: Session,
    current_user: User,
    *,
    patient_id: uuid.UUID | None,
    consultation_id: uuid.UUID | None,
    status_filter: LabOrderStatus | None,
) -> list[LabOrder]:
    """GET /api/v1/lab/orders. Caller (routers/lab.py) is responsible for the
    "at least one of patient_id/consultation_id/status required" 422 --
    that's request validation, not a search-logic concern (same split as
    `patient_service.search_patients`).

    Doctor-ownership filtering is done IN SQL (a join against `Consultation`
    filtered to `doctor_id == current_user.doctor_id`), not by fetching every
    matching order and filtering in Python -- this means a doctor's query
    never even materializes another doctor's order row, rather than
    fetching-then-discarding it. This is a judgment call worth flagging: the
    PRP doesn't pin implementation strategy, but SQL-side filtering is
    strictly safer (no window where an unauthorized row exists in memory)
    and is the same approach `patient_service.search_patients` uses for its
    own filters.

    A doctor caller with no matching Doctor record (`current_user.doctor_id
    is None`) gets an empty list here, NOT a 403 -- unlike `create_order`/
    `get_order`, which give an explicit 403 for that data-consistency case.
    This is deliberately different: those two endpoints target one specific
    resource where a clear 403 is the more useful signal, whereas this is a
    filter/search endpoint where "no matching data" is an ordinary,
    unremarkable outcome (same reasoning `patient_service.search_patients`
    uses -- a search with no results isn't an error).
    """
    query = select(LabOrder)

    if current_user.role == UserRole.doctor:
        query = query.join(Consultation, Consultation.id == LabOrder.consultation_id).where(
            Consultation.doctor_id == current_user.doctor_id
        )
    # nurse/lab_tech/system_admin: unrestricted (role-only gating, per PRP).

    if patient_id is not None:
        query = query.where(LabOrder.patient_id == patient_id)
    if consultation_id is not None:
        query = query.where(LabOrder.consultation_id == consultation_id)
    if status_filter is not None:
        query = query.where(LabOrder.status == status_filter)

    query = query.order_by(LabOrder.created_at.desc())
    return list(db.execute(query).scalars().all())


def transition_order(
    db: Session,
    current_user: User,
    order_id: uuid.UUID,
    payload: TransitionRequest,
) -> LabOrder:
    """PATCH /api/v1/lab/orders/{id}/transition. Validation order (each a
    distinct HTTP status, per the Phase 2 brief):
      1. 404 if the order doesn't exist.
      2. 403 if the caller's role isn't permitted for the SPECIFIC
         `to_status` requested (`_TRANSITION_ROLES`) -- checked here, not
         just via a blanket `require_role` at the router.
      3. 409 if `lab_workflow.is_legal_transition` rejects the transition
         (skip/backward/repeat/already-attached -- a state conflict).
      4. 409 if `to_status == "collected"` and a `LabSample` already exists
         for this order.
      5. 422 if `lab_workflow.validate_result_for_transition` rejects the
         accompanying `result` (missing at verify, or present anywhere else).
    On success: create the `LabSample` row (`collected`) or update the
    existing one (`processing`/`verified`/`attached`), flip `order.status`,
    audit-log, commit.

    Uses `_get_order_or_404_for_update` (a `SELECT ... FOR UPDATE` row lock),
    not the plain `_get_order_or_404` -- REVIEW-AGENT finding H1/M1
    (PRPs/lab-module-prp.md Phase 3): two concurrent transition requests for
    the same order must serialize here, or both could act on the same
    stale `order.status` and either create duplicate `LabSample` rows or
    silently overwrite each other's update. See that helper's docstring.
    """
    order = _get_order_or_404_for_update(db, order_id)
    requested = LabOrderStatus(payload.to_status)

    allowed_roles = _TRANSITION_ROLES[requested]
    if current_user.role not in allowed_roles:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"role={current_user.role.value} may not perform the transition to '{requested.value}'",
        )

    if not lab_workflow.is_legal_transition(order.status, requested):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot transition a lab order from '{order.status.value}' to '{requested.value}'",
        )

    sample = _get_sample_for_order(db, order.id)

    if requested == LabOrderStatus.collected:
        if sample is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "a sample has already been collected for this order",
            )
    else:
        # Every transition past `collected` must have a sample row already
        # (created by the `ordered -> collected` transition below) -- this
        # module is the sole writer of LabSample rows, and `is_legal_transition`
        # already confirmed `order.status` has moved past `ordered`. A `None`
        # sample here indicates a data inconsistency, not a normal client
        # error -- surfaced as a 409 rather than silently proceeding.
        if sample is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "no sample record exists for this order despite its status; data inconsistency",
            )

    validation_error = lab_workflow.validate_result_for_transition(requested, payload.result)
    if validation_error is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, validation_error)

    now = datetime.now(timezone.utc)
    old_status = order.status

    if requested == LabOrderStatus.collected:
        sample = LabSample(
            lab_order_id=order.id,
            collected_by=current_user.id,
            collected_at=now,
        )
        db.add(sample)
    elif requested == LabOrderStatus.processing:
        assert sample is not None  # guaranteed by the check above
        sample.processed_at = now
    elif requested == LabOrderStatus.verified:
        assert sample is not None
        sample.verified_by = current_user.id
        sample.verified_at = now
        sample.result = payload.result  # PHI -- EncryptedString handles at-rest encryption
    elif requested == LabOrderStatus.attached:
        assert sample is not None
        sample.attached_to_emr_at = now

    order.status = requested
    db.add(order)

    # PHI note: the audit metadata records only whether a result was
    # submitted at this transition, never the value itself -- the value
    # already lives in `lab_samples.result_encrypted`; duplicating it into
    # the audit log's JSONB metadata would be a second, less-controlled place
    # PHI is stored for no operational benefit (see module docstring).
    record_audit_event(
        db,
        branch_id=None,  # lab_orders/lab_samples have no branch_id column
        actor_user_id=current_user.id,
        action=f"lab_order.transitioned_to_{requested.value}",
        resource_type="lab_order",
        resource_id=str(order.id),
        metadata={
            "old_status": old_status.value,
            "new_status": requested.value,
            "actor": str(current_user.id),
            "result_submitted": payload.result is not None,
        },
    )

    try:
        db.commit()
    except IntegrityError as exc:
        # Defense-in-depth alongside the FOR UPDATE lock above (REVIEW-AGENT
        # finding H1): if the `lab_samples.lab_order_id` UNIQUE constraint
        # is ever tripped despite the lock (e.g. a future refactor drops the
        # lock, or a connection pooling/isolation-level surprise), fail
        # cleanly with a 409 instead of an unhandled IntegrityError -> 500 --
        # same pattern `patient_service.py`'s MRN-collision retry and
        # `scheduling_engine.py`'s exclusion-constraint catch already use in
        # this codebase for exactly this class of belt-and-suspenders case.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a sample has already been collected for this order",
        ) from exc
    db.refresh(order)

    # HMS Project Completion Prompt gap: notification-hub-prp.md's own
    # module docstring names "Lab, Pharmacy, Ward, and Billing... never call
    # event_publisher.publish(...) anywhere" as an explicit, on-purpose scope
    # boundary at the time -- closing it here for lab reports specifically.
    # Fired at `verified` (the transition that actually sets `sample.result`)
    # rather than `attached` (a later filing step with no new information for
    # the patient/doctor) -- see notification_engine.py's new
    # `_handle_lab_report_ready` handler for the recipient resolution.
    if requested == LabOrderStatus.verified:
        event_publisher.publish(
            "lab.report_ready",
            {"lab_order_id": str(order.id), "patient_id": str(order.patient_id)},
        )

    return order


# --- Response shaping ---------------------------------------------------------


def _shape_one(
    current_user: User, order: LabOrder, sample: LabSample | None
) -> LabOrderResponse | LabOrderResponseNoResult:
    """The actual role-branch, factored out so both the single-order shaper
    and the batched list shaper below share one implementation -- the
    restriction can't be forgotten on one endpoint and not the other."""
    if current_user.role == UserRole.nurse:
        return LabOrderResponseNoResult(
            id=order.id,
            consultation_id=order.consultation_id,
            patient_id=order.patient_id,
            test_code=order.test_code,
            ordered_by=order.ordered_by,
            status=order.status,
            created_at=order.created_at,
            sample=LabSampleResponseNoResult.model_validate(sample) if sample is not None else None,
        )

    return LabOrderResponse(
        id=order.id,
        consultation_id=order.consultation_id,
        patient_id=order.patient_id,
        test_code=order.test_code,
        ordered_by=order.ordered_by,
        status=order.status,
        created_at=order.created_at,
        sample=LabSampleResponse.model_validate(sample) if sample is not None else None,
    )


def shape_lab_order_response(
    db: Session, current_user: User, order: LabOrder
) -> LabOrderResponse | LabOrderResponseNoResult:
    """The single place a `LabOrder` ORM row becomes an API response, used by
    the create/get-by-id/transition endpoints (all three return exactly one
    order) -- same "one function shapes the response" discipline as
    `patient_service.shape_patient_response` / `consultation_service.
    build_consultation_response`. See `shape_lab_order_responses` for the
    list endpoint's batched sibling."""
    sample = _get_sample_for_order(db, order.id)
    return _shape_one(current_user, order, sample)


def shape_lab_order_responses(
    db: Session, current_user: User, orders: list[LabOrder]
) -> list[LabOrderResponse | LabOrderResponseNoResult]:
    """Batched sibling of `shape_lab_order_response` for the list endpoint --
    fetches every relevant `LabSample` in one query (keyed by
    `lab_order_id`) rather than one query per order, same N+1 avoidance
    `consultation_service._prescriptions_with_items` uses."""
    if not orders:
        return []
    order_ids = [order.id for order in orders]
    samples = list(
        db.execute(select(LabSample).where(LabSample.lab_order_id.in_(order_ids))).scalars().all()
    )
    samples_by_order_id = {sample.lab_order_id: sample for sample in samples}
    return [_shape_one(current_user, order, samples_by_order_id.get(order.id)) for order in orders]
