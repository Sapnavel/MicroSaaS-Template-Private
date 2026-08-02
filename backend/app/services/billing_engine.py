"""Billing / Ledger / Insurance Claims engine (PRPs/billing-module-prp.md,
"ENGINE DESIGN"). Six functions -- `create_invoice`, `add_invoice_item`,
`split_invoice`, `set_claim_state`, `mark_invoice_paid`, `void_invoice` --
each a single `SELECT ... FOR UPDATE` + mutate + commit + audit-log inside
one transaction, exactly the "commit + audit-log owned by the engine
function itself" shape `services/ward_engine.py` uses (as opposed to Lab's
"engine computes, service persists" split) -- there is no separate
persistence layer here because there is no expensive allocation computation
to unit-test in isolation the way Pharmacy's FEFO `allocate_fefo` needs.

Two judgment calls this module makes explicit, both spelled out in the
PRP's design decisions (read there for the full reasoning; this is the
short version so this file's shape makes sense read on its own):

1. NO REDIS LOCK MANAGER HERE -- deliberately, unlike every other engine
   in this codebase (`scheduling_engine`, `ward_engine`, `pharmacy_engine`).
   Those modules coordinate a time-range or quantity resource across
   independent processes *before* a DB constraint can arbitrate. Billing's
   hazard is different: a plain read-modify-write ledger balance on ONE row
   (`invoices.total_amount`). A Postgres `SELECT ... FOR UPDATE` inside a
   single transaction is both necessary and sufficient for that -- the
   actual authoritative guard against the one race that matters (billing
   the same clinical event twice, possibly on two different invoices) is
   the DB-level `invoice_items_source_unique` UNIQUE constraint, which
   needs no lock at all. Do not import `core.locking` into this module.

2. EVERY LOCKED READ USES `execution_options(populate_existing=True)` --
   REVIEW-AGENT finding C1 (PRPs/billing-module-prp.md Phase 3): this file's
   own functions have no reason to do an unlocked pre-read on the same row
   before their `SELECT ... FOR UPDATE` (there's no Redis lock key to derive
   first, the `invoice_id`/`claim_id` path parameter already IS the lock
   target) -- but `services/billing_service.py`'s callers DO an unlocked
   `db.get()` first, on the SAME `Session`, to have a row for `authorize()`
   to check the caller's branch against before the engine is ever invoked.
   That means the identity-map hazard `ward_engine.py`'s `discharge_patient`/
   `transfer_patient` was originally found to have (REVIEW-AGENT finding H1,
   PRPs/ward-bed-ot-module-prp.md Phase 3 -- an unlocked `db.get()` followed
   by a same-session `SELECT ... FOR UPDATE` returning the SAME stale,
   identity-mapped Python object instead of a refreshed row) applies here
   too, just at the service/engine seam rather than within one function.
   `populate_existing=True` on every locked re-fetch below is the same fix
   `ward_engine.py` settled on -- it forces SQLAlchemy to overwrite the
   cached object's attributes from the row Postgres just locked, regardless
   of what already sat in the session's identity map.

`total_amount` handling (PRP design decision #2): `add_invoice_item` NEVER
does `invoice.total_amount += item.amount`. It inserts the item, flushes,
then recomputes `total_amount` from `SELECT SUM(amount) FROM invoice_items
WHERE invoice_id = :id` (via `func.coalesce(func.sum(...), 0)` so a
zero-item invoice reads `0`, not `NULL`) and assigns that fresh value. This
is the same "never trust a derived field as anything but a cache recomputed
fresh every write" discipline the PRP calls out to avoid the class of bug
`Bed.status` drift represents in the Ward module.
"""

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import record_audit_event
from app.models.billing import ClaimState, InsuranceClaim, Invoice, InvoiceItem, InvoiceStatus
from app.models.consultation import Consultation, Prescription
from app.models.lab import LabOrder, LabOrderStatus
from app.models.ward import Admission

logger = logging.getLogger(__name__)


class InvoiceNotFoundError(Exception):
    """Raised when an `invoice_id` passed to any of these functions does not
    exist. Callers turn this into a 404."""


class InvalidInvoiceStateError(Exception):
    """Raised when a mutation is attempted against an `Invoice` whose
    current `status` does not permit it -- adding an item to a
    non-`open` invoice, splitting a non-`open` invoice, or marking
    paid/voiding an invoice that is not `open` or `split`. Callers turn
    this into a 409."""


class SourceEventNotFoundError(Exception):
    """Raised by `add_invoice_item` when the referenced `(source_type,
    source_id)` row does not exist (including an unrecognized
    `source_type`, since there is then nothing to look up). Callers turn
    this into a 404."""


class SourceEventPatientMismatchError(Exception):
    """Raised by `add_invoice_item` when the referenced source event's
    `patient_id` does not match the invoice's `patient_id` -- a source_id
    that is real but belongs to someone else's clinical record. Callers
    turn this into a 400."""


class SourceEventNotChargeableError(Exception):
    """Raised by `add_invoice_item` when the referenced source event exists
    and belongs to the right patient, but fails its per-type chargeable
    check (PRP design decision #4): an unfinished `Consultation`
    (`ended_at is None`), a `LabOrder` not yet `attached`, a `Prescription`
    not yet `finalized`, or an `Admission` not yet discharged. Callers turn
    this into a 409."""


class DuplicateChargeError(Exception):
    """Raised when the `invoice_items_source_unique` DB constraint rejects
    an insert -- this exact `(source_type, source_id)` has already been
    billed on some invoice, possibly a different one than the one being
    charged now. This is the authoritative guard (PRP design decision #1),
    not merely an in-memory pre-check. Callers turn this into a 409."""


class ClaimAmountMismatchError(Exception):
    """Raised by `split_invoice` when `claim_amount + patient_copay` does
    not exactly equal `invoice.total_amount` -- exact decimal equality is
    required, this is money, not a value to round-tolerate. Callers turn
    this into a 422."""


class ClaimNotFoundError(Exception):
    """Raised when a `claim_id` passed to `set_claim_state` does not exist.
    Callers turn this into a 404."""


class IllegalClaimStateTransition(Exception):
    """Raised by `set_claim_state` when `(current, requested)` is not one of
    `_LEGAL_CLAIM_TRANSITIONS` -- `denied` and `paid` are both terminal,
    never a legal source state. Carries `.current`/`.requested` so callers
    can build a precise error message. Callers turn this into a 409."""

    def __init__(self, *, current: ClaimState, requested: ClaimState) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"illegal claim state transition: {current.value} -> {requested.value}")


# --- Legal claim state transitions (mirrors ward_engine.py's
# `_LEGAL_BED_TRANSITIONS` shape: a set of legal (current, requested)
# pairs). `denied` and `paid` never appear as a key (source) here -- both
# terminal. See PRP ENGINE DESIGN, `set_claim_state`. -----------------------
_LEGAL_CLAIM_TRANSITIONS: set[tuple[ClaimState, ClaimState]] = {
    (ClaimState.submitted, ClaimState.adjudicating),
    (ClaimState.submitted, ClaimState.denied),
    (ClaimState.adjudicating, ClaimState.approved),
    (ClaimState.adjudicating, ClaimState.denied),
    (ClaimState.approved, ClaimState.paid),
}

# Source types `add_invoice_item` recognizes, mapped to the ORM class to
# look the referenced row up in. Matches schema.sql's
# `invoice_items_source_type_valid` CHECK constraint's allowed values
# exactly.
_SOURCE_TYPE_MODELS = {
    "consultation": Consultation,
    "lab_order": LabOrder,
    "prescription": Prescription,
    "admission": Admission,
}


def _is_chargeable(source_type: str, source_row) -> bool:
    """Per-type chargeable check, PRP design decision #4. `Prescription`'s
    check is `status == "finalized"`, NOT `"dispensed"` -- nothing in this
    codebase today ever transitions a `Prescription.status` past
    `"finalized"` (Pharmacy's dispense workflow does not write back to
    `Prescription.status`), so `"finalized"` is honestly just "was this
    prescription ever written," not "was it actually filled." Documented
    here plainly rather than silently treated as if it meant "dispensed."
    """
    if source_type == "consultation":
        return source_row.ended_at is not None
    if source_type == "lab_order":
        return source_row.status == LabOrderStatus.attached
    if source_type == "prescription":
        return source_row.status == "finalized"
    if source_type == "admission":
        return source_row.discharged_at is not None
    return False


def create_invoice(
    db: Session,
    *,
    patient_id: uuid.UUID,
    branch_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> Invoice:
    """Open a new, empty invoice (`status="open"`, `total_amount=0`).

    No `FOR UPDATE` needed -- nothing to race against yet, the row does not
    exist until this function returns.
    """
    invoice = Invoice(
        patient_id=patient_id,
        branch_id=branch_id,
        status=InvoiceStatus.open.value,
        total_amount=Decimal("0"),
    )
    db.add(invoice)
    db.flush()

    record_audit_event(
        db,
        branch_id=branch_id,
        actor_user_id=actor_user_id,
        action="billing.invoice_created",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={"patient_id": str(patient_id), "branch_id": str(branch_id)},
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def add_invoice_item(
    db: Session,
    *,
    invoice_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    description: str,
    amount: Decimal,
    actor_user_id: uuid.UUID,
) -> Invoice:
    """Bill one chargeable clinical event onto an open invoice.

    Raises:
        InvoiceNotFoundError: no such invoice (404).
        InvalidInvoiceStateError: `invoice.status != "open"` (409).
        SourceEventNotFoundError: no such `(source_type, source_id)` row, or
            `source_type` is not one of the four recognized values (404).
        SourceEventPatientMismatchError: the referenced row's `patient_id`
            does not match `invoice.patient_id` (400).
        SourceEventNotChargeableError: the referenced row fails its
            per-type chargeable check, see `_is_chargeable` (409).
        DuplicateChargeError: the `invoice_items_source_unique` DB
            constraint rejected the insert -- this event was already
            billed, possibly on a different invoice (409).
    """
    invoice = db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFoundError(f"invoice {invoice_id} not found")
    if invoice.status != InvoiceStatus.open.value:
        raise InvalidInvoiceStateError(
            f"invoice {invoice_id} is not open (status={invoice.status!r}); cannot add items"
        )

    model = _SOURCE_TYPE_MODELS.get(source_type)
    if model is None:
        raise SourceEventNotFoundError(f"unrecognized source_type {source_type!r}")

    source_row = db.get(model, source_id)
    if source_row is None:
        raise SourceEventNotFoundError(f"{source_type} {source_id} not found")
    if source_row.patient_id != invoice.patient_id:
        raise SourceEventPatientMismatchError(
            f"{source_type} {source_id} belongs to a different patient than invoice {invoice_id}"
        )
    if not _is_chargeable(source_type, source_row):
        raise SourceEventNotChargeableError(f"{source_type} {source_id} is not yet chargeable")

    item = InvoiceItem(
        invoice_id=invoice.id,
        source_type=source_type,
        source_id=source_id,
        description=description,
        amount=amount,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("invoice_items_source_unique rejected duplicate charge: %s", exc)
        raise DuplicateChargeError(
            f"{source_type} {source_id} has already been billed on some invoice"
        ) from exc

    # Recompute, never increment -- PRP design decision #2.
    total = db.execute(
        select(func.coalesce(func.sum(InvoiceItem.amount), 0)).where(InvoiceItem.invoice_id == invoice.id)
    ).scalar_one()
    invoice.total_amount = Decimal(total)
    db.add(invoice)

    record_audit_event(
        db,
        branch_id=invoice.branch_id,
        actor_user_id=actor_user_id,
        action="billing.item_added",
        resource_type="invoice_item",
        resource_id=str(item.id),
        metadata={"source_type": source_type, "source_id": str(source_id), "amount": str(amount)},
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def split_invoice(
    db: Session,
    *,
    invoice_id: uuid.UUID,
    payer_name: str,
    claim_amount: Decimal,
    patient_copay: Decimal,
    actor_user_id: uuid.UUID,
) -> Invoice:
    """Split an open invoice's total between an insurer claim and the
    patient's copay, creating a new `InsuranceClaim(state="submitted")`.

    Raises:
        InvoiceNotFoundError: no such invoice (404).
        InvalidInvoiceStateError: `invoice.status != "open"` (409).
        ClaimAmountMismatchError: `claim_amount + patient_copay !=
            invoice.total_amount`, exact decimal equality (422).
    """
    invoice = db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFoundError(f"invoice {invoice_id} not found")
    if invoice.status != InvoiceStatus.open.value:
        raise InvalidInvoiceStateError(f"invoice {invoice_id} is not open (status={invoice.status!r}); cannot split")

    if claim_amount + patient_copay != invoice.total_amount:
        raise ClaimAmountMismatchError(
            f"claim_amount ({claim_amount}) + patient_copay ({patient_copay}) != "
            f"invoice.total_amount ({invoice.total_amount})"
        )

    claim = InsuranceClaim(
        invoice_id=invoice.id,
        payer_name=payer_name,
        claim_amount=claim_amount,
        patient_copay=patient_copay,
        state=ClaimState.submitted.value,
    )
    db.add(claim)

    invoice.status = InvoiceStatus.split.value
    db.add(invoice)

    db.flush()

    record_audit_event(
        db,
        branch_id=invoice.branch_id,
        actor_user_id=actor_user_id,
        action="billing.invoice_split",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={
            "claim_id": str(claim.id),
            "payer_name": payer_name,
            "claim_amount": str(claim_amount),
            "patient_copay": str(patient_copay),
        },
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def set_claim_state(
    db: Session,
    *,
    claim_id: uuid.UUID,
    requested_state: ClaimState,
    actor_user_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> InsuranceClaim:
    """Transition an `InsuranceClaim`'s adjudication state.

    `branch_id` is caller-supplied (the service layer already loads the
    claim's parent `Invoice` to `authorize()` against it, since
    `InsuranceClaim` has no `branch_id` of its own) purely so the audit
    event below can be branch-attributed like every other billing action --
    REVIEW-AGENT finding M1 (PRPs/billing-module-prp.md Phase 3). Optional
    (defaults to `None`) since this function has no other need to resolve
    the parent invoice itself.

    Does NOT touch `invoice.status` -- see PRP design decision #5: there is
    no `payments` ledger table in this schema, so a claim reaching `paid`
    does not by itself mean the invoice's whole balance (insurer + copay)
    has been collected. `mark_invoice_paid` is the sole, explicit path to
    `Invoice.status == "paid"`.

    Raises:
        ClaimNotFoundError: no such claim (404).
        IllegalClaimStateTransition: `(current, requested)` not in
            `_LEGAL_CLAIM_TRANSITIONS` (409).
    """
    claim = db.execute(
        select(InsuranceClaim)
        .where(InsuranceClaim.id == claim_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if claim is None:
        raise ClaimNotFoundError(f"claim {claim_id} not found")

    current = ClaimState(claim.state)
    if (current, requested_state) not in _LEGAL_CLAIM_TRANSITIONS:
        raise IllegalClaimStateTransition(current=current, requested=requested_state)

    claim.state = requested_state.value
    db.add(claim)

    record_audit_event(
        db,
        branch_id=branch_id,
        actor_user_id=actor_user_id,
        action="billing.claim_state_changed",
        resource_type="insurance_claim",
        resource_id=str(claim.id),
        metadata={"from": current.value, "to": requested_state.value},
    )
    db.commit()
    db.refresh(claim)
    return claim


def mark_invoice_paid(
    db: Session,
    *,
    invoice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> Invoice:
    """Explicitly mark an invoice as fully settled (insurer payment + patient
    copay both actually collected, outside this system's own tracking -- see
    PRP design decision #5).

    Raises:
        InvoiceNotFoundError: no such invoice (404).
        InvalidInvoiceStateError: `invoice.status not in ("open", "split")`
            (409).
    """
    invoice = db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFoundError(f"invoice {invoice_id} not found")
    if invoice.status not in (InvoiceStatus.open.value, InvoiceStatus.split.value):
        raise InvalidInvoiceStateError(
            f"invoice {invoice_id} cannot be marked paid from status={invoice.status!r}"
        )

    invoice.status = InvoiceStatus.paid.value
    db.add(invoice)

    record_audit_event(
        db,
        branch_id=invoice.branch_id,
        actor_user_id=actor_user_id,
        action="billing.invoice_paid",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={},
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def void_invoice(
    db: Session,
    *,
    invoice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> Invoice:
    """Void an invoice that should never have existed -- reachable from
    `open` or `split` only. A `paid` invoice needs a real refund process
    (out of scope): voiding is not a way to undo a completed payment.

    Raises:
        InvoiceNotFoundError: no such invoice (404).
        InvalidInvoiceStateError: `invoice.status not in ("open", "split")`
            (409).
    """
    invoice = db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFoundError(f"invoice {invoice_id} not found")
    if invoice.status not in (InvoiceStatus.open.value, InvoiceStatus.split.value):
        raise InvalidInvoiceStateError(f"invoice {invoice_id} cannot be voided from status={invoice.status!r}")

    invoice.status = InvoiceStatus.void.value
    db.add(invoice)

    record_audit_event(
        db,
        branch_id=invoice.branch_id,
        actor_user_id=actor_user_id,
        action="billing.invoice_voided",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={},
    )
    db.commit()
    db.refresh(invoice)
    return invoice
