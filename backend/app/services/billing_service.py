"""Billing, Ledger & Insurance Claims module business logic
(PRPs/billing-module-prp.md, Phase 2). Router (routers/billing.py) stays
thin and delegates here -- same "business logic lives in services/, router
is thin" split as services/ward_service.py / services/pharmacy_service.py.

Same authorize-before-mutate discipline as services/ward_service.py (read
its module docstring for the full "why"): every mutating function here
resolves/loads the affected `Invoice` (or, for `set_claim_state`, the
`Invoice` a claim belongs to) and calls `core.security.authorize` BEFORE
invoking any `billing_engine` function. `billing_engine`'s functions commit
internally, so -- exactly like Ward, and unlike Pharmacy's `dispense`
(REVIEW-AGENT finding, PRPs/pharmacy-module-prp.md Phase 3) -- there is no
"authorize after the fact, but before commit" middle ground: authorize-first
is the only ordering that keeps a denied caller from ever causing a state
mutation.

`_BranchScoped` mirrors `ward_service._BranchScoped`/`pharmacy_service._BranchScoped`
exactly: a minimal stand-in resource for `authorize()`'s tenant guard when
there is no single ORM row to check against yet (invoice creation, where the
`Invoice` doesn't exist until after the engine call; and the two
patient-scoped list endpoints, which aggregate across many rows rather than
one).

Every branch resolution here goes through `core.security.get_caller_branch_id`,
never `current_user.branch_id` directly -- a real bug (REVIEW-AGENT finding
H1, PRPs/pharmacy-module-prp.md Phase 3) was found and fixed in Pharmacy for
exactly the direct-attribute mistake; see `get_caller_branch_id`'s own
docstring for why the two answers can legitimately disagree mid-session.

`list_chargeable_events` deliberately treats `Prescription.status ==
"finalized"` as the chargeable signal for prescriptions -- this is honestly
just "was this prescription ever written," not "was it actually filled" (see
PRP design decision #4: nothing in this codebase today ever transitions a
`Prescription.status` past `"finalized"`, Pharmacy's dispense workflow does
not write back to it). This is a known, deliberate limitation, not a bug.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import authorize, get_caller_branch_id
from app.models.appointment import Appointment
from app.models.billing import ClaimState, InsuranceClaim, Invoice, InvoiceItem
from app.models.consultation import Consultation, Prescription
from app.models.lab import LabOrder, LabOrderStatus
from app.models.user import User, UserRole
from app.models.ward import Admission, Bed, Ward
from app.schemas.billing import (
    ChargeableEventResponse,
    ClaimStateUpdate,
    InsuranceClaimResponse,
    InvoiceCreate,
    InvoiceDetailResponse,
    InvoiceItemCreate,
    InvoiceItemResponse,
    InvoiceResponse,
    InvoiceSplitCreate,
)
from app.services import billing_engine


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard when there
    is no single ORM row to check against -- invoice creation (the `Invoice`
    doesn't exist until after `billing_engine.create_invoice` returns) and
    the two patient-scoped list endpoints. Same pattern as
    `ward_service._BranchScoped`/`pharmacy_service._BranchScoped`."""

    branch_id: uuid.UUID


def _get_invoice_or_404(db: Session, invoice_id: uuid.UUID) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"invoice {invoice_id} not found")
    return invoice


def _get_claim_or_404(db: Session, claim_id: uuid.UUID) -> InsuranceClaim:
    claim = db.get(InsuranceClaim, claim_id)
    if claim is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"claim {claim_id} not found")
    return claim


def _resolve_branch_filter(current_user: User, branch_id_param: uuid.UUID | None) -> uuid.UUID | None:
    """Shared branch-resolution helper for the two patient-scoped list
    endpoints. `system_admin` may omit `branch_id` to mean "all branches";
    every other role MUST supply `branch_id` and it MUST match their own
    (token-claims-preferred, via `get_caller_branch_id` -- never
    `current_user.branch_id` directly). Mismatch or omission for a
    non-admin caller is 422 -- mirrors `ward_service._resolve_branch_filter`/
    `pharmacy_service._resolve_branch_filter` exactly, do not reinvent a
    looser version."""
    if current_user.role == UserRole.system_admin:
        return branch_id_param

    caller_branch_id = get_caller_branch_id(current_user)
    if caller_branch_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "your account has no branch assigned; contact an administrator",
        )
    caller_branch_uuid = uuid.UUID(caller_branch_id)
    if branch_id_param is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "branch_id is required")
    if branch_id_param != caller_branch_uuid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "branch_id in the request does not match your assigned branch",
        )
    return caller_branch_uuid


def create_invoice(db: Session, current_user: User, payload: InvoiceCreate) -> InvoiceResponse:
    """POST /api/v1/billing/invoices. Authorizes against a `_BranchScoped`
    stand-in for the requested `branch_id` BEFORE calling
    `billing_engine.create_invoice` -- the `Invoice` row doesn't exist yet
    for `authorize()` to check against directly."""
    authorize(current_user, "billing", "write", _BranchScoped(branch_id=payload.branch_id))

    invoice = billing_engine.create_invoice(
        db,
        patient_id=payload.patient_id,
        branch_id=payload.branch_id,
        actor_user_id=current_user.id,
    )
    return InvoiceResponse.model_validate(invoice)


def get_invoice(db: Session, current_user: User, invoice_id: uuid.UUID) -> InvoiceDetailResponse:
    """GET /api/v1/billing/invoices/{id}. `Invoice` has a real `branch_id`
    column, so it is passed directly to `authorize()` -- no `_BranchScoped`
    stand-in needed here."""
    invoice = _get_invoice_or_404(db, invoice_id)
    authorize(current_user, "billing", "read", invoice)

    items = db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    ).scalars().all()
    claim = db.execute(
        select(InsuranceClaim).where(InsuranceClaim.invoice_id == invoice_id)
    ).scalar_one_or_none()

    return InvoiceDetailResponse(
        invoice=InvoiceResponse.model_validate(invoice),
        items=[InvoiceItemResponse.model_validate(item) for item in items],
        claim=InsuranceClaimResponse.model_validate(claim) if claim is not None else None,
    )


def list_patient_invoices(
    db: Session,
    current_user: User,
    patient_id: uuid.UUID,
    branch_id_param: uuid.UUID | None,
) -> list[InvoiceResponse]:
    """GET /api/v1/billing/patients/{patient_id}/invoices?branch_id="""
    branch_id = _resolve_branch_filter(current_user, branch_id_param)
    if branch_id is not None:
        authorize(current_user, "billing", "read", _BranchScoped(branch_id=branch_id))
    # else: system_admin querying across every branch -- authorize() already
    # bypasses the tenant guard for system_admin (core/security.py's
    # _admin_bypass), and no other role can reach this branch (non-admin
    # always resolves to a concrete branch_id above).

    query = select(Invoice).where(Invoice.patient_id == patient_id)
    if branch_id is not None:
        query = query.where(Invoice.branch_id == branch_id)

    invoices = db.execute(query).scalars().all()
    return [InvoiceResponse.model_validate(invoice) for invoice in invoices]


def list_chargeable_events(
    db: Session,
    current_user: User,
    patient_id: uuid.UUID,
    branch_id_param: uuid.UUID | None,
) -> list[ChargeableEventResponse]:
    """GET /api/v1/billing/patients/{patient_id}/chargeable-events?branch_id=

    Pure read -- lives here, not in `billing_engine`, since there is no
    mutation and no `FOR UPDATE` lock needed (same reasoning Pharmacy's own
    list endpoints use). For each of the four source types, finds rows
    belonging to `patient_id`, chargeable per the exact same per-type checks
    `billing_engine._is_chargeable` uses (re-derived here directly on the
    query, not imported, since these are plain column comparisons), resolved
    to the requested `branch_id`, and not yet present as a `(source_type,
    source_id)` row in `invoice_items` (the double-billing guard's read-side
    mirror).

    NOTE on `Prescription.status == "finalized"`: this is deliberately just
    "was this prescription ever written," not "was it actually filled" --
    nothing in this codebase today ever transitions a `Prescription.status`
    past `"finalized"` (PRP design decision #4). Documented here plainly
    rather than silently treated as if it meant "dispensed."

    No suggested amount -- this system has no fee schedule; `billing_admin`
    enters the amount manually on `add_invoice_item` (PRP design decision
    #6).
    """
    branch_id = _resolve_branch_filter(current_user, branch_id_param)
    if branch_id is not None:
        authorize(current_user, "billing", "read", _BranchScoped(branch_id=branch_id))

    events: list[ChargeableEventResponse] = []

    def _not_already_billed(source_type: str, source_id_col):
        return ~(
            select(InvoiceItem.id)
            .where(InvoiceItem.source_type == source_type, InvoiceItem.source_id == source_id_col)
            .exists()
        )

    # --- Consultations -----------------------------------------------------
    consultation_query = (
        select(Consultation, Appointment.branch_id)
        .join(Appointment, Appointment.id == Consultation.appointment_id)
        .where(
            Consultation.patient_id == patient_id,
            Consultation.ended_at.is_not(None),
            _not_already_billed("consultation", Consultation.id),
        )
    )
    if branch_id is not None:
        consultation_query = consultation_query.where(Appointment.branch_id == branch_id)
    for consultation, _branch_id in db.execute(consultation_query).all():
        events.append(
            ChargeableEventResponse(
                source_type="consultation",
                source_id=consultation.id,
                suggested_description=f"Consultation on {consultation.started_at.date()}",
                event_date=consultation.started_at,
            )
        )

    # --- Lab orders ----------------------------------------------------------
    lab_order_query = (
        select(LabOrder, Appointment.branch_id)
        .join(Consultation, Consultation.id == LabOrder.consultation_id)
        .join(Appointment, Appointment.id == Consultation.appointment_id)
        .where(
            LabOrder.patient_id == patient_id,
            LabOrder.status == LabOrderStatus.attached,
            _not_already_billed("lab_order", LabOrder.id),
        )
    )
    if branch_id is not None:
        lab_order_query = lab_order_query.where(Appointment.branch_id == branch_id)
    for lab_order, _branch_id in db.execute(lab_order_query).all():
        events.append(
            ChargeableEventResponse(
                source_type="lab_order",
                source_id=lab_order.id,
                suggested_description=f"Lab order: {lab_order.test_code}",
                event_date=lab_order.created_at,
            )
        )

    # --- Prescriptions ---------------------------------------------------
    prescription_query = (
        select(Prescription, Appointment.branch_id)
        .join(Consultation, Consultation.id == Prescription.consultation_id)
        .join(Appointment, Appointment.id == Consultation.appointment_id)
        .where(
            Prescription.patient_id == patient_id,
            Prescription.status == "finalized",
            _not_already_billed("prescription", Prescription.id),
        )
    )
    if branch_id is not None:
        prescription_query = prescription_query.where(Appointment.branch_id == branch_id)
    for prescription, _branch_id in db.execute(prescription_query).all():
        events.append(
            ChargeableEventResponse(
                source_type="prescription",
                source_id=prescription.id,
                suggested_description=f"Prescription #{prescription.id}",
                event_date=prescription.created_at,
            )
        )

    # --- Admissions ------------------------------------------------------
    admission_query = (
        select(Admission, Bed.label, Ward.branch_id)
        .join(Bed, Bed.id == Admission.bed_id)
        .join(Ward, Ward.id == Bed.ward_id)
        .where(
            Admission.patient_id == patient_id,
            Admission.discharged_at.is_not(None),
            _not_already_billed("admission", Admission.id),
        )
    )
    if branch_id is not None:
        admission_query = admission_query.where(Ward.branch_id == branch_id)
    for admission, bed_label, _branch_id in db.execute(admission_query).all():
        events.append(
            ChargeableEventResponse(
                source_type="admission",
                source_id=admission.id,
                suggested_description=f"Admission: bed {bed_label}",
                event_date=admission.discharged_at,
            )
        )

    return events


def add_invoice_item(
    db: Session, current_user: User, invoice_id: uuid.UUID, payload: InvoiceItemCreate
) -> InvoiceResponse:
    """POST /api/v1/billing/invoices/{id}/items. Authorizes against the
    loaded `Invoice` BEFORE calling `billing_engine.add_invoice_item`."""
    invoice = _get_invoice_or_404(db, invoice_id)
    authorize(current_user, "billing", "write", invoice)

    updated = billing_engine.add_invoice_item(
        db,
        invoice_id=invoice_id,
        source_type=payload.source_type,
        source_id=payload.source_id,
        description=payload.description,
        amount=payload.amount,
        actor_user_id=current_user.id,
    )
    return InvoiceResponse.model_validate(updated)


def split_invoice(
    db: Session, current_user: User, invoice_id: uuid.UUID, payload: InvoiceSplitCreate
) -> InvoiceResponse:
    """POST /api/v1/billing/invoices/{id}/split. Authorizes against the
    loaded `Invoice` BEFORE calling `billing_engine.split_invoice`."""
    invoice = _get_invoice_or_404(db, invoice_id)
    authorize(current_user, "billing", "write", invoice)

    updated = billing_engine.split_invoice(
        db,
        invoice_id=invoice_id,
        payer_name=payload.payer_name,
        claim_amount=payload.claim_amount,
        patient_copay=payload.patient_copay,
        actor_user_id=current_user.id,
    )
    return InvoiceResponse.model_validate(updated)


def set_claim_state(
    db: Session, current_user: User, claim_id: uuid.UUID, payload: ClaimStateUpdate
) -> InsuranceClaimResponse:
    """PATCH /api/v1/billing/claims/{id}/state. Claims have no `branch_id`
    of their own -- resolve through `invoice_id` to the parent `Invoice` and
    authorize against THAT, BEFORE calling `billing_engine.set_claim_state`."""
    claim = _get_claim_or_404(db, claim_id)
    invoice = _get_invoice_or_404(db, claim.invoice_id)
    authorize(current_user, "billing", "write", invoice)

    updated = billing_engine.set_claim_state(
        db,
        claim_id=claim_id,
        requested_state=ClaimState(payload.state),
        actor_user_id=current_user.id,
        branch_id=invoice.branch_id,
    )
    return InsuranceClaimResponse.model_validate(updated)


def mark_invoice_paid(db: Session, current_user: User, invoice_id: uuid.UUID) -> InvoiceResponse:
    """PATCH /api/v1/billing/invoices/{id}/mark-paid. Authorizes against the
    loaded `Invoice` BEFORE calling `billing_engine.mark_invoice_paid`."""
    invoice = _get_invoice_or_404(db, invoice_id)
    authorize(current_user, "billing", "write", invoice)

    updated = billing_engine.mark_invoice_paid(db, invoice_id=invoice_id, actor_user_id=current_user.id)
    return InvoiceResponse.model_validate(updated)


def void_invoice(db: Session, current_user: User, invoice_id: uuid.UUID) -> InvoiceResponse:
    """PATCH /api/v1/billing/invoices/{id}/void. Authorizes against the
    loaded `Invoice` BEFORE calling `billing_engine.void_invoice`."""
    invoice = _get_invoice_or_404(db, invoice_id)
    authorize(current_user, "billing", "write", invoice)

    updated = billing_engine.void_invoice(db, invoice_id=invoice_id, actor_user_id=current_user.id)
    return InvoiceResponse.model_validate(updated)
