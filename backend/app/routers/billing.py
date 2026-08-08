"""Billing, Ledger & Insurance Claims module (PRPs/billing-module-prp.md,
Phase 2). Business logic lives in services/billing_service.py -- this
module is request validation, role gating, exception-to-status-code
mapping, and response shaping only, matching the pattern in
routers/wards.py / routers/pharmacy.py.

Exception -> status code mapping (plain `HTTPException` throughout -- none
of billing's exceptions carry extra structured fields the way Ward's
`IllegalBedStatusTransition`/`OTConflictError` did, so no hand-built
`JSONResponse` is needed here):
    InvoiceNotFoundError / ClaimNotFoundError /
        SourceEventNotFoundError                -> 404
    SourceEventPatientMismatchError              -> 400
    InvalidInvoiceStateError / SourceEventNotChargeableError /
        DuplicateChargeError / IllegalClaimStateTransition -> 409
    ClaimAmountMismatchError / DiscountExceedsTotalError -> 422

`front_desk` is read-only (checkout needs to see balance due, never
mutates billing state) -- no `doctor`/`nurse`/`lab_tech`/`pharmacist` access
at all, per the PRP's ENDPOINTS table.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.billing import (
    ChargeableEventResponse,
    ClaimListItemResponse,
    ClaimStateUpdate,
    InsuranceClaimResponse,
    InvoiceAdjustmentsUpdate,
    InvoiceCreate,
    InvoiceDetailResponse,
    InvoiceItemCreate,
    InvoiceResponse,
    InvoiceSplitCreate,
)
from app.services import billing_service
from app.services.billing_engine import (
    ClaimAmountMismatchError,
    ClaimNotFoundError,
    DiscountExceedsTotalError,
    DuplicateChargeError,
    IllegalClaimStateTransition,
    InvalidInvoiceStateError,
    InvoiceNotFoundError,
    SourceEventNotChargeableError,
    SourceEventNotFoundError,
    SourceEventPatientMismatchError,
)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

_WRITE_ROLES = ("billing_admin", "system_admin")
_READ_ROLES = ("billing_admin", "front_desk", "system_admin")
# Matches ClaimsPage.tsx's route gate (frontend/src/App.tsx's /billing/claims
# route) -- tighter than _READ_ROLES above (no front_desk).
_CLAIMS_READ_ROLES = ("billing_admin", "system_admin")


@router.get("/claims")
def list_claims(
    branch_id: uuid.UUID,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_CLAIMS_READ_ROLES)),
) -> list[ClaimListItemResponse]:
    """GET /api/v1/billing/claims?branch_id=&state= (frontend
    UUID-to-dropdown conversion follow-up, backend phase)."""
    return billing_service.list_claims(db, current_user, branch_id, state)


@router.post("/invoices", status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: InvoiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InvoiceResponse:
    return billing_service.create_invoice(db, current_user, payload)


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> InvoiceDetailResponse:
    return billing_service.get_invoice(db, current_user, invoice_id)


@router.post("/invoices/{invoice_id}/send-reminder")
def send_payment_reminder(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> InvoiceResponse:
    """HMS Project Completion Prompt gap: "Payment reminders" had no trigger
    anywhere in the notification pipeline -- see billing_service.
    send_payment_reminder's docstring for why this is staff-triggered, not
    automatic. Gated the same as every other read on this router
    (`_READ_ROLES`, includes front_desk): this doesn't mutate the invoice,
    it publishes a notification event."""
    return billing_service.send_payment_reminder(db, current_user, invoice_id)


@router.get("/patients/{patient_id}/invoices")
def list_patient_invoices(
    patient_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> list[InvoiceResponse]:
    return billing_service.list_patient_invoices(db, current_user, patient_id, branch_id)


@router.get("/patients/{patient_id}/chargeable-events")
def list_chargeable_events(
    patient_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> list[ChargeableEventResponse]:
    return billing_service.list_chargeable_events(db, current_user, patient_id, branch_id)


@router.post("/invoices/{invoice_id}/items", status_code=status.HTTP_201_CREATED)
def add_invoice_item(
    invoice_id: uuid.UUID,
    payload: InvoiceItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InvoiceResponse:
    try:
        return billing_service.add_invoice_item(db, current_user, invoice_id, payload)
    except (InvoiceNotFoundError, SourceEventNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except SourceEventPatientMismatchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (InvalidInvoiceStateError, SourceEventNotChargeableError, DuplicateChargeError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.patch("/invoices/{invoice_id}/adjustments")
def apply_invoice_adjustments(
    invoice_id: uuid.UUID,
    payload: InvoiceAdjustmentsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InvoiceResponse:
    """HMS Project Completion Prompt gap ("tax and discount handling")."""
    try:
        return billing_service.apply_invoice_adjustments(db, current_user, invoice_id, payload)
    except InvoiceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidInvoiceStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except DiscountExceedsTotalError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/invoices/{invoice_id}/receipt")
def get_invoice_receipt(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> Response:
    """HMS Project Completion Prompt gap ("receipt generation"). Read-only,
    same role gate as `get_invoice` -- a receipt is just a formatted view
    of data the caller can already read."""
    pdf_bytes = billing_service.generate_receipt_pdf(db, current_user, invoice_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="invoice-{invoice_id}.pdf"'},
    )


@router.post("/invoices/{invoice_id}/split")
def split_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceSplitCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InvoiceResponse:
    try:
        return billing_service.split_invoice(db, current_user, invoice_id, payload)
    except InvoiceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidInvoiceStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ClaimAmountMismatchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.patch("/claims/{claim_id}/state")
def set_claim_state(
    claim_id: uuid.UUID,
    payload: ClaimStateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InsuranceClaimResponse:
    try:
        return billing_service.set_claim_state(db, current_user, claim_id, payload)
    except (InvoiceNotFoundError, ClaimNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except IllegalClaimStateTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.patch("/invoices/{invoice_id}/mark-paid")
def mark_invoice_paid(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InvoiceResponse:
    try:
        return billing_service.mark_invoice_paid(db, current_user, invoice_id)
    except InvoiceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidInvoiceStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.patch("/invoices/{invoice_id}/void")
def void_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> InvoiceResponse:
    try:
        return billing_service.void_invoice(db, current_user, invoice_id)
    except InvoiceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidInvoiceStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
