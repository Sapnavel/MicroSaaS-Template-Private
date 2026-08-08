"""Tests for `app.services.billing_engine` (PRPs/billing-module-prp.md,
Phase 3). Runs against a real Postgres (see tests/conftest.py's module
docstring), reusing fixtures from prior modules' TEST-AGENTs (`db`, `branch`,
`patient`, `in_progress_appointment`, `consultation`, `doctor_record`,
`staff_user`, `ward`, `bed`) plus this file's own local helpers for building
chargeable source rows (`LabOrder`, `Prescription`, `Admission`) directly via
the ORM -- the same "direct ORM insert, not through another module's API"
convention `test_pharmacy.py`'s `_make_item`/`_make_batch` and
`test_ward_engine.py`'s `_make_patient` already establish, since these tests
only need a valid, correctly-shaped row to bill against, not to re-exercise
Consultation/Lab/Ward's own workflows.

This file is pure engine-level testing (calling `billing_engine.*` functions
directly against a real `Session`, not through the HTTP layer) -- router-level
status-code-mapping and authorize()/RBAC/branch-tenant-isolation tests live in
test_billing_router.py, mirroring the test_ward_engine.py / test_wards_router.py
split.
"""

import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.billing import ClaimState, Invoice, InvoiceItem, InvoiceStatus
from app.models.consultation import Consultation, Prescription
from app.models.lab import LabOrder, LabOrderStatus
from app.models.patient import Patient
from app.models.ward import Admission
from app.services import billing_engine
from app.services.billing_engine import (
    ClaimAmountMismatchError,
    ClaimNotFoundError,
    DuplicateChargeError,
    IllegalClaimStateTransition,
    InvalidInvoiceStateError,
    InvoiceNotFoundError,
    SourceEventNotChargeableError,
    SourceEventNotFoundError,
    SourceEventPatientMismatchError,
)
from tests.conftest import TestSessionLocal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end_consultation(db, consultation: Consultation) -> Consultation:
    """Marks an already-started `consultation` fixture as finished
    (`ended_at` set) -- the chargeable signal `_is_chargeable` checks for
    `source_type == "consultation"`."""
    consultation.ended_at = datetime.now(timezone.utc)
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    return consultation


def _make_lab_order(
    db,
    *,
    consultation: Consultation,
    patient: Patient,
    ordered_by: uuid.UUID,
    status: LabOrderStatus = LabOrderStatus.attached,
    test_code: str = "CBC",
) -> LabOrder:
    lab_order = LabOrder(
        consultation_id=consultation.id,
        patient_id=patient.id,
        test_code=test_code,
        ordered_by=ordered_by,
        status=status,
    )
    db.add(lab_order)
    db.commit()
    db.refresh(lab_order)
    return lab_order


def _make_prescription(
    db, *, consultation: Consultation, patient: Patient, status: str = "finalized"
) -> Prescription:
    prescription = Prescription(consultation_id=consultation.id, patient_id=patient.id, status=status)
    db.add(prescription)
    db.commit()
    db.refresh(prescription)
    return prescription


def _make_admission(db, *, patient: Patient, bed, admitted_by: uuid.UUID, discharged: bool) -> Admission:
    start = datetime.now(timezone.utc) - timedelta(days=2)
    if discharged:
        end = start + timedelta(days=1)
        stay_range = f"[{start.isoformat()},{end.isoformat()})"
        discharged_at = end
    else:
        stay_range = f"[{start.isoformat()},)"
        discharged_at = None
    admission = Admission(
        patient_id=patient.id,
        bed_id=bed.id,
        stay_range=stay_range,
        admitted_by=admitted_by,
        discharged_at=discharged_at,
    )
    db.add(admission)
    db.commit()
    db.refresh(admission)
    return admission


@pytest.fixture
def other_patient(db) -> Patient:
    """A SECOND, independent `Patient` row -- needed for
    `SourceEventPatientMismatchError` (a source event belonging to someone
    else's clinical record) and the cross-invoice duplicate-charge test."""
    p = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Other Billing Test Patient",
        dob=date(1990, 3, 20),
        sex="M",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture
def open_invoice(db, patient, branch, staff_user) -> Invoice:
    return billing_engine.create_invoice(
        db, patient_id=patient.id, branch_id=branch.id, actor_user_id=staff_user.id
    )


@pytest.fixture
def ended_consultation(db, consultation) -> Consultation:
    """`consultation` (conftest.py) belongs to `patient`, started but not
    ended. Chargeable per `_is_chargeable`'s consultation check once
    `ended_at` is set."""
    return _end_consultation(db, consultation)


@pytest.fixture
def attached_lab_order(db, ended_consultation, patient, staff_user) -> LabOrder:
    return _make_lab_order(
        db, consultation=ended_consultation, patient=patient, ordered_by=staff_user.id
    )


# ---------------------------------------------------------------------------
# 1. Full invoice lifecycle: create -> add items (recomputed, not additive
#    drift) -> split -> mark-paid.
# ---------------------------------------------------------------------------


def test_create_invoice_is_open_and_zero(open_invoice):
    assert open_invoice.status == InvoiceStatus.open.value
    assert open_invoice.total_amount == Decimal("0")


def test_add_invoice_item_recomputes_total_not_increments(
    db, open_invoice, ended_consultation, patient, staff_user
):
    """Two sequential adds must leave `total_amount` exactly equal to the
    sum of the two item amounts -- proves `add_invoice_item` recomputes
    `SUM(invoice_items.amount)` fresh rather than doing
    `invoice.total_amount += item.amount` (PRP design decision #2). Amounts
    are chosen with cents that would reveal float/rounding drift if the
    implementation ever stopped using `Decimal` throughout."""
    lab_order = _make_lab_order(db, consultation=ended_consultation, patient=patient, ordered_by=staff_user.id)
    prescription = _make_prescription(db, consultation=ended_consultation, patient=patient)

    after_first = billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("123.45"),
        actor_user_id=staff_user.id,
    )
    assert after_first.total_amount == Decimal("123.45")

    after_second = billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="lab_order",
        source_id=lab_order.id,
        description="Lab: CBC",
        amount=Decimal("67.89"),
        actor_user_id=staff_user.id,
    )
    # Exact decimal equality -- 123.45 + 67.89 = 191.34, not a drifted value.
    assert after_second.total_amount == Decimal("191.34")

    # Third add for good measure -- keeps proving recomputation, not
    # incrementing, holds across more than two writes.
    after_third = billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="prescription",
        source_id=prescription.id,
        description="Prescription fee",
        amount=Decimal("10.01"),
        actor_user_id=staff_user.id,
    )
    assert after_third.total_amount == Decimal("201.35")

    # Independent verification straight from the DB, not the returned object.
    db.expire_all()
    reloaded = db.get(Invoice, open_invoice.id)
    assert reloaded.total_amount == Decimal("201.35")
    items = db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == open_invoice.id)).scalars().all()
    assert sum((item.amount for item in items), Decimal("0")) == Decimal("201.35")


def test_add_invoice_item_never_uses_increment_assignment():
    """Static confirmation, not just behavioral inference -- the PRP calls
    out this exact bug class (`Bed.status`-style derived-field drift) by
    name. Mirrors test_pharmacy.py's/test_ward_engine.py's own source-level
    checks (e.g. `"with_for_update()" in source`) rather than trusting a
    passing behavioral test alone."""
    import inspect

    source = inspect.getsource(billing_engine.add_invoice_item)
    assert "total_amount +=" not in source
    assert "func.sum" in source


def test_full_lifecycle_create_items_split_mark_paid(db, open_invoice, ended_consultation, patient, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )

    split = billing_engine.split_invoice(
        db,
        invoice_id=open_invoice.id,
        payer_name="Acme Insurance",
        claim_amount=Decimal("70.00"),
        patient_copay=Decimal("30.00"),
        actor_user_id=staff_user.id,
    )
    assert split.status == InvoiceStatus.split.value

    paid = billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)
    assert paid.status == InvoiceStatus.paid.value


# ---------------------------------------------------------------------------
# 2. Void: reachable from open and split; rejected from paid.
# ---------------------------------------------------------------------------


def test_void_invoice_from_open(db, open_invoice, staff_user):
    voided = billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)
    assert voided.status == InvoiceStatus.void.value


def test_void_invoice_from_split(db, open_invoice, ended_consultation, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("50.00"),
        actor_user_id=staff_user.id,
    )
    billing_engine.split_invoice(
        db,
        invoice_id=open_invoice.id,
        payer_name="Acme Insurance",
        claim_amount=Decimal("50.00"),
        patient_copay=Decimal("0.00"),
        actor_user_id=staff_user.id,
    )

    voided = billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)
    assert voided.status == InvoiceStatus.void.value


def test_void_invoice_rejected_from_paid(db, open_invoice, staff_user):
    billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)


# ---------------------------------------------------------------------------
# 3. Claim state machine: legal chain, illegal skips, denied/paid terminal.
# ---------------------------------------------------------------------------


def _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("200.00"),
        actor_user_id=staff_user.id,
    )
    return billing_engine.split_invoice(
        db,
        invoice_id=open_invoice.id,
        payer_name="Acme Insurance",
        claim_amount=Decimal("150.00"),
        patient_copay=Decimal("50.00"),
        actor_user_id=staff_user.id,
    )


def _get_claim(db, invoice_id):
    from app.models.billing import InsuranceClaim

    return db.execute(select(InsuranceClaim).where(InsuranceClaim.invoice_id == invoice_id)).scalar_one()


def test_claim_legal_chain_submitted_to_paid(db, open_invoice, ended_consultation, staff_user):
    _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user)
    claim = _get_claim(db, open_invoice.id)
    assert claim.state == ClaimState.submitted.value

    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.adjudicating, actor_user_id=staff_user.id
    )
    assert claim.state == ClaimState.adjudicating.value

    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.approved, actor_user_id=staff_user.id
    )
    assert claim.state == ClaimState.approved.value

    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.paid, actor_user_id=staff_user.id
    )
    assert claim.state == ClaimState.paid.value


def test_claim_illegal_skip_submitted_to_paid(db, open_invoice, ended_consultation, staff_user):
    _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user)
    claim = _get_claim(db, open_invoice.id)

    with pytest.raises(IllegalClaimStateTransition) as exc_info:
        billing_engine.set_claim_state(
            db, claim_id=claim.id, requested_state=ClaimState.paid, actor_user_id=staff_user.id
        )
    assert exc_info.value.current == ClaimState.submitted
    assert exc_info.value.requested == ClaimState.paid


def test_claim_illegal_skip_submitted_to_approved(db, open_invoice, ended_consultation, staff_user):
    _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user)
    claim = _get_claim(db, open_invoice.id)

    with pytest.raises(IllegalClaimStateTransition) as exc_info:
        billing_engine.set_claim_state(
            db, claim_id=claim.id, requested_state=ClaimState.approved, actor_user_id=staff_user.id
        )
    assert exc_info.value.current == ClaimState.submitted
    assert exc_info.value.requested == ClaimState.approved


def test_claim_denied_is_terminal(db, open_invoice, ended_consultation, staff_user):
    _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user)
    claim = _get_claim(db, open_invoice.id)

    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.denied, actor_user_id=staff_user.id
    )
    assert claim.state == ClaimState.denied.value

    for target in (ClaimState.adjudicating, ClaimState.approved, ClaimState.paid, ClaimState.submitted):
        with pytest.raises(IllegalClaimStateTransition):
            billing_engine.set_claim_state(
                db, claim_id=claim.id, requested_state=target, actor_user_id=staff_user.id
            )


def test_claim_paid_is_terminal(db, open_invoice, ended_consultation, staff_user):
    _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user)
    claim = _get_claim(db, open_invoice.id)
    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.adjudicating, actor_user_id=staff_user.id
    )
    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.approved, actor_user_id=staff_user.id
    )
    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.paid, actor_user_id=staff_user.id
    )
    assert claim.state == ClaimState.paid.value

    for target in (ClaimState.adjudicating, ClaimState.approved, ClaimState.denied, ClaimState.submitted):
        with pytest.raises(IllegalClaimStateTransition):
            billing_engine.set_claim_state(
                db, claim_id=claim.id, requested_state=target, actor_user_id=staff_user.id
            )


def test_set_claim_state_does_not_touch_invoice_status(db, open_invoice, ended_consultation, staff_user):
    """PRP design decision #5: a claim reaching `paid` must NOT flip
    `invoice.status` -- only the explicit `mark_invoice_paid` may."""
    _split_invoice_for_claim(db, open_invoice, ended_consultation, staff_user)
    claim = _get_claim(db, open_invoice.id)
    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.adjudicating, actor_user_id=staff_user.id
    )
    claim = billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.approved, actor_user_id=staff_user.id
    )
    billing_engine.set_claim_state(
        db, claim_id=claim.id, requested_state=ClaimState.paid, actor_user_id=staff_user.id
    )

    db.expire_all()
    invoice = db.get(Invoice, open_invoice.id)
    assert invoice.status == InvoiceStatus.split.value  # unchanged by the claim reaching "paid"


def test_set_claim_state_not_found(db, staff_user):
    with pytest.raises(ClaimNotFoundError):
        billing_engine.set_claim_state(
            db, claim_id=uuid.uuid4(), requested_state=ClaimState.adjudicating, actor_user_id=staff_user.id
        )


# ---------------------------------------------------------------------------
# 4. DuplicateChargeError: same source event twice -- same invoice AND
#    across two DIFFERENT invoices for the same patient.
# ---------------------------------------------------------------------------


def test_duplicate_charge_same_invoice(db, open_invoice, ended_consultation, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )

    with pytest.raises(DuplicateChargeError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="consultation",
            source_id=ended_consultation.id,
            description="Consultation fee (again)",
            amount=Decimal("100.00"),
            actor_user_id=staff_user.id,
        )


def test_duplicate_charge_across_two_different_invoices_same_patient(
    db, open_invoice, ended_consultation, patient, branch, staff_user
):
    """The authoritative guard is the DB's `invoice_items_source_unique`
    constraint, not merely an in-memory per-invoice check -- billing the
    SAME clinical event onto a SECOND invoice for the SAME patient must
    still be rejected."""
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )

    second_invoice = billing_engine.create_invoice(
        db, patient_id=patient.id, branch_id=branch.id, actor_user_id=staff_user.id
    )

    with pytest.raises(DuplicateChargeError) as exc_info:
        billing_engine.add_invoice_item(
            db,
            invoice_id=second_invoice.id,
            source_type="consultation",
            source_id=ended_consultation.id,
            description="Consultation fee (different invoice)",
            amount=Decimal("100.00"),
            actor_user_id=staff_user.id,
        )
    # Proves the real DB constraint fired, not just an app-level guess.
    from sqlalchemy.exc import IntegrityError

    assert isinstance(exc_info.value.__cause__, IntegrityError)

    # The second invoice must remain untouched (still open, still 0) --
    # the rejected insert must not have left partial state behind.
    db.expire_all()
    reloaded_second = db.get(Invoice, second_invoice.id)
    assert reloaded_second.status == InvoiceStatus.open.value
    assert reloaded_second.total_amount == Decimal("0")


# ---------------------------------------------------------------------------
# 5. SourceEventNotChargeableError -- all four source types.
# ---------------------------------------------------------------------------


def test_not_chargeable_unfinished_consultation(db, open_invoice, consultation, staff_user):
    # `consultation` fixture is started but NOT ended (ended_at is None).
    with pytest.raises(SourceEventNotChargeableError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="consultation",
            source_id=consultation.id,
            description="Consultation fee",
            amount=Decimal("100.00"),
            actor_user_id=staff_user.id,
        )


def test_not_chargeable_lab_order_not_attached(db, open_invoice, ended_consultation, patient, staff_user):
    lab_order = _make_lab_order(
        db,
        consultation=ended_consultation,
        patient=patient,
        ordered_by=staff_user.id,
        status=LabOrderStatus.ordered,
    )
    with pytest.raises(SourceEventNotChargeableError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="lab_order",
            source_id=lab_order.id,
            description="Lab: CBC",
            amount=Decimal("50.00"),
            actor_user_id=staff_user.id,
        )


def test_not_chargeable_prescription_not_finalized(db, open_invoice, ended_consultation, patient, staff_user):
    prescription = _make_prescription(db, consultation=ended_consultation, patient=patient, status="draft")
    with pytest.raises(SourceEventNotChargeableError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="prescription",
            source_id=prescription.id,
            description="Prescription fee",
            amount=Decimal("20.00"),
            actor_user_id=staff_user.id,
        )


def test_not_chargeable_admission_not_discharged(db, open_invoice, patient, bed, staff_user):
    admission = _make_admission(db, patient=patient, bed=bed, admitted_by=staff_user.id, discharged=False)
    with pytest.raises(SourceEventNotChargeableError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="admission",
            source_id=admission.id,
            description="Admission fee",
            amount=Decimal("500.00"),
            actor_user_id=staff_user.id,
        )


def test_chargeable_admission_discharged_succeeds(db, open_invoice, patient, bed, staff_user):
    """Positive control for the admission chargeable check -- proves the
    409 above is really about `discharged_at`, not some unrelated setup
    mistake."""
    admission = _make_admission(db, patient=patient, bed=bed, admitted_by=staff_user.id, discharged=True)
    updated = billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="admission",
        source_id=admission.id,
        description="Admission fee",
        amount=Decimal("500.00"),
        actor_user_id=staff_user.id,
    )
    assert updated.total_amount == Decimal("500.00")


# ---------------------------------------------------------------------------
# 6. SourceEventPatientMismatchError.
# ---------------------------------------------------------------------------


def test_source_event_patient_mismatch(
    db, open_invoice, branch, doctor_record, room, other_patient, staff_user
):
    """`open_invoice` belongs to `patient` (conftest.py); the consultation
    below belongs to `other_patient` -- a real row, just the wrong owner."""
    from app.models.appointment import Appointment, AppointmentStatus

    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    other_appointment = Appointment(
        branch_id=branch.id,
        patient_id=other_patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(other_appointment)
    db.commit()
    db.refresh(other_appointment)

    other_consultation = Consultation(
        appointment_id=other_appointment.id,
        doctor_id=other_appointment.doctor_id,
        patient_id=other_appointment.patient_id,
        symptoms="Unrelated",
    )
    db.add(other_consultation)
    db.commit()
    db.refresh(other_consultation)
    _end_consultation(db, other_consultation)

    with pytest.raises(SourceEventPatientMismatchError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="consultation",
            source_id=other_consultation.id,
            description="Consultation fee",
            amount=Decimal("100.00"),
            actor_user_id=staff_user.id,
        )


# ---------------------------------------------------------------------------
# 7. SourceEventNotFoundError -- random UUID, and unrecognized source_type.
# ---------------------------------------------------------------------------


def test_source_event_not_found_random_uuid(db, open_invoice, staff_user):
    with pytest.raises(SourceEventNotFoundError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="lab_order",
            source_id=uuid.uuid4(),
            description="Lab: unknown",
            amount=Decimal("10.00"),
            actor_user_id=staff_user.id,
        )


def test_source_event_not_found_unrecognized_source_type(db, open_invoice, staff_user):
    with pytest.raises(SourceEventNotFoundError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="not_a_real_type",
            source_id=uuid.uuid4(),
            description="???",
            amount=Decimal("10.00"),
            actor_user_id=staff_user.id,
        )


# ---------------------------------------------------------------------------
# 8. ClaimAmountMismatchError -- too high and too low.
# ---------------------------------------------------------------------------


def test_split_claim_amount_mismatch_too_high(db, open_invoice, ended_consultation, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )
    with pytest.raises(ClaimAmountMismatchError):
        billing_engine.split_invoice(
            db,
            invoice_id=open_invoice.id,
            payer_name="Acme Insurance",
            claim_amount=Decimal("80.00"),
            patient_copay=Decimal("30.00"),  # 110.00 != 100.00
            actor_user_id=staff_user.id,
        )


def test_split_claim_amount_mismatch_too_low(db, open_invoice, ended_consultation, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )
    with pytest.raises(ClaimAmountMismatchError):
        billing_engine.split_invoice(
            db,
            invoice_id=open_invoice.id,
            payer_name="Acme Insurance",
            claim_amount=Decimal("50.00"),
            patient_copay=Decimal("30.00"),  # 80.00 != 100.00
            actor_user_id=staff_user.id,
        )

    # Invoice must remain open/untouched after a rejected split.
    db.expire_all()
    reloaded = db.get(Invoice, open_invoice.id)
    assert reloaded.status == InvoiceStatus.open.value


# ---------------------------------------------------------------------------
# 9. InvalidInvoiceStateError on every open/open-or-split-only action.
# ---------------------------------------------------------------------------


def test_add_item_rejected_on_split_invoice(db, open_invoice, ended_consultation, patient, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )
    billing_engine.split_invoice(
        db,
        invoice_id=open_invoice.id,
        payer_name="Acme Insurance",
        claim_amount=Decimal("100.00"),
        patient_copay=Decimal("0.00"),
        actor_user_id=staff_user.id,
    )

    other_lab_order = _make_lab_order(db, consultation=ended_consultation, patient=patient, ordered_by=staff_user.id)
    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="lab_order",
            source_id=other_lab_order.id,
            description="Lab: CBC",
            amount=Decimal("10.00"),
            actor_user_id=staff_user.id,
        )


def test_add_item_rejected_on_paid_invoice(db, open_invoice, ended_consultation, staff_user):
    billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="consultation",
            source_id=ended_consultation.id,
            description="Consultation fee",
            amount=Decimal("100.00"),
            actor_user_id=staff_user.id,
        )


def test_add_item_rejected_on_void_invoice(db, open_invoice, ended_consultation, staff_user):
    billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=open_invoice.id,
            source_type="consultation",
            source_id=ended_consultation.id,
            description="Consultation fee",
            amount=Decimal("100.00"),
            actor_user_id=staff_user.id,
        )


def test_split_rejected_on_already_split_invoice(db, open_invoice, ended_consultation, staff_user):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )
    billing_engine.split_invoice(
        db,
        invoice_id=open_invoice.id,
        payer_name="Acme Insurance",
        claim_amount=Decimal("100.00"),
        patient_copay=Decimal("0.00"),
        actor_user_id=staff_user.id,
    )

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.split_invoice(
            db,
            invoice_id=open_invoice.id,
            payer_name="Acme Insurance",
            claim_amount=Decimal("100.00"),
            patient_copay=Decimal("0.00"),
            actor_user_id=staff_user.id,
        )


def test_mark_paid_rejected_on_paid_invoice(db, open_invoice, staff_user):
    billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)


def test_mark_paid_rejected_on_void_invoice(db, open_invoice, staff_user):
    billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)


def test_void_rejected_on_paid_invoice(db, open_invoice, staff_user):
    billing_engine.mark_invoice_paid(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)


# ---------------------------------------------------------------------------
# 404s: InvoiceNotFoundError / ClaimNotFoundError for nonexistent primary
# keys, across every mutating function -- cheap coverage, and this is
# exactly what the router layer must map to a 404 (see test_billing_router.py).
# ---------------------------------------------------------------------------


def test_add_invoice_item_invoice_not_found(db, staff_user):
    with pytest.raises(InvoiceNotFoundError):
        billing_engine.add_invoice_item(
            db,
            invoice_id=uuid.uuid4(),
            source_type="lab_order",
            source_id=uuid.uuid4(),
            description="x",
            amount=Decimal("1.00"),
            actor_user_id=staff_user.id,
        )


def test_split_invoice_not_found(db, staff_user):
    with pytest.raises(InvoiceNotFoundError):
        billing_engine.split_invoice(
            db,
            invoice_id=uuid.uuid4(),
            payer_name="Acme",
            claim_amount=Decimal("1.00"),
            patient_copay=Decimal("0.00"),
            actor_user_id=staff_user.id,
        )


def test_mark_invoice_paid_not_found(db, staff_user):
    with pytest.raises(InvoiceNotFoundError):
        billing_engine.mark_invoice_paid(db, invoice_id=uuid.uuid4(), actor_user_id=staff_user.id)


def test_void_invoice_not_found(db, staff_user):
    with pytest.raises(InvoiceNotFoundError):
        billing_engine.void_invoice(db, invoice_id=uuid.uuid4(), actor_user_id=staff_user.id)


# ---------------------------------------------------------------------------
# 10. Concurrency: concurrent add_invoice_item calls on the SAME invoice
#     (different source events), separate DB sessions/threads -- mirrors
#     test_pharmacy.py's FEFO concurrency test structure. Proves
#     `total_amount` ends up correct (no lost updates) purely from Postgres
#     `SELECT ... FOR UPDATE` on the invoice row -- PRP design decision #1.
# ---------------------------------------------------------------------------


def test_concurrent_add_invoice_item_total_amount_has_no_lost_updates(
    db, branch, patient, staff_user, doctor_record, room
):
    """Sets up ONE open invoice and N distinct chargeable `LabOrder` rows
    (same consultation, different orders -- no unique constraint prevents
    several lab orders per consultation), then fires N threads, each on its
    own `TestSessionLocal()` connection, each calling
    `billing_engine.add_invoice_item` for a DIFFERENT `(source_type,
    source_id)` against the SAME invoice concurrently.

    Without the `SELECT ... FOR UPDATE` on the invoice row, two threads could
    both read the pre-write item list, each insert their own item, and each
    independently recompute `SUM(invoice_items.amount)` without seeing the
    other's not-yet-committed insert -- whichever thread commits last would
    silently overwrite `total_amount` with a value that excludes the other
    thread's charge (a classic lost update on the derived cache), even though
    `add_invoice_item` never uses `+=` in-process. The row lock forces true
    serialization: each thread's `SELECT ... FOR UPDATE` blocks until the
    previous thread's transaction commits and releases it, so every
    recompute's `SUM` sees every previously-committed sibling item. Final
    `total_amount` must equal the exact sum of every item actually inserted.
    """
    setup_db = TestSessionLocal()
    try:
        invoice = billing_engine.create_invoice(
            setup_db, patient_id=patient.id, branch_id=branch.id, actor_user_id=staff_user.id
        )
        invoice_id = invoice.id

        start = datetime.now(timezone.utc)
        end = start + timedelta(minutes=30)
        from app.models.appointment import Appointment, AppointmentStatus

        appointment = Appointment(
            branch_id=branch.id,
            patient_id=patient.id,
            doctor_id=doctor_record.id,
            room_id=room.id,
            time_range=f"[{start.isoformat()},{end.isoformat()})",
            status=AppointmentStatus.in_progress,
        )
        setup_db.add(appointment)
        setup_db.commit()
        setup_db.refresh(appointment)

        shared_consultation = Consultation(
            appointment_id=appointment.id,
            doctor_id=appointment.doctor_id,
            patient_id=appointment.patient_id,
            symptoms="Concurrency test",
        )
        setup_db.add(shared_consultation)
        setup_db.commit()
        setup_db.refresh(shared_consultation)
        consultation_id = shared_consultation.id

        n_threads = 8
        lab_order_ids: list[uuid.UUID] = []
        amounts: list[Decimal] = []
        for i in range(n_threads):
            lab_order = LabOrder(
                consultation_id=consultation_id,
                patient_id=patient.id,
                test_code=f"TEST-{i}",
                ordered_by=staff_user.id,
                status=LabOrderStatus.attached,
            )
            setup_db.add(lab_order)
            setup_db.commit()
            setup_db.refresh(lab_order)
            lab_order_ids.append(lab_order.id)
            amounts.append(Decimal(f"{10 + i}.{(i * 7) % 100:02d}"))

        actor_id = staff_user.id
    finally:
        setup_db.close()

    results: dict[int, object] = {}
    errors: dict[int, Exception] = {}

    def _worker(index: int) -> None:
        worker_db = TestSessionLocal()
        try:
            updated = billing_engine.add_invoice_item(
                worker_db,
                invoice_id=invoice_id,
                source_type="lab_order",
                source_id=lab_order_ids[index],
                description=f"Lab: TEST-{index}",
                amount=amounts[index],
                actor_user_id=actor_id,
            )
            results[index] = updated.total_amount
        except Exception as exc:  # noqa: BLE001 - surfaced to the main thread
            errors[index] = exc
        finally:
            worker_db.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"unexpected errors from concurrent add_invoice_item calls: {errors}"
    assert len(results) == n_threads

    expected_total = sum(amounts, Decimal("0"))

    verify_db = TestSessionLocal()
    try:
        final_invoice = verify_db.get(Invoice, invoice_id)
        final_items = (
            verify_db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)).scalars().all()
        )
    finally:
        verify_db.close()

    # No lost items: every thread's insert actually landed.
    assert len(final_items) == n_threads
    # No lost updates: the final total_amount is the EXACT sum of every
    # committed item, not just whichever thread happened to commit last.
    assert final_invoice.total_amount == expected_total
    assert sum((item.amount for item in final_items), Decimal("0")) == expected_total


def test_add_invoice_item_uses_select_for_update():
    """Static confirmation that the concurrency guarantee above is actually
    backed by a real row lock, not an accident of test timing."""
    import inspect

    source = inspect.getsource(billing_engine.add_invoice_item)
    assert "with_for_update()" in source


# ---------------------------------------------------------------------------
# 8. apply_invoice_adjustments -- HMS Project Completion Prompt gap
#    ("tax and discount handling").
# ---------------------------------------------------------------------------


def test_apply_invoice_adjustments_sets_tax_and_discount(
    db, open_invoice, ended_consultation, staff_user
):
    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )

    updated = billing_engine.apply_invoice_adjustments(
        db,
        invoice_id=open_invoice.id,
        tax_rate_percent=Decimal("10.00"),
        discount_amount=Decimal("20.00"),
        actor_user_id=staff_user.id,
    )
    assert updated.tax_rate_percent == Decimal("10.00")
    assert updated.discount_amount == Decimal("20.00")
    # total_amount (the itemized subtotal) is untouched by adjustments --
    # only tax_rate_percent/discount_amount change.
    assert updated.total_amount == Decimal("100.00")


def test_apply_invoice_adjustments_discount_exceeding_total_raises(db, open_invoice, staff_user):
    """`open_invoice` has `total_amount == 0` (no items added) -- any
    positive discount already exceeds it."""
    with pytest.raises(billing_engine.DiscountExceedsTotalError):
        billing_engine.apply_invoice_adjustments(
            db,
            invoice_id=open_invoice.id,
            tax_rate_percent=None,
            discount_amount=Decimal("0.01"),
            actor_user_id=staff_user.id,
        )


def test_apply_invoice_adjustments_rejected_on_non_open_invoice(db, open_invoice, staff_user):
    billing_engine.void_invoice(db, invoice_id=open_invoice.id, actor_user_id=staff_user.id)

    with pytest.raises(InvalidInvoiceStateError):
        billing_engine.apply_invoice_adjustments(
            db,
            invoice_id=open_invoice.id,
            tax_rate_percent=Decimal("5.00"),
            discount_amount=Decimal("0"),
            actor_user_id=staff_user.id,
        )


def test_apply_invoice_adjustments_not_found(db, staff_user):
    with pytest.raises(InvoiceNotFoundError):
        billing_engine.apply_invoice_adjustments(
            db,
            invoice_id=uuid.uuid4(),
            tax_rate_percent=None,
            discount_amount=Decimal("0"),
            actor_user_id=staff_user.id,
        )


def test_invoice_response_grand_total_computed_discount_then_tax(
    db, open_invoice, ended_consultation, staff_user
):
    """`InvoiceResponse.grand_total` is `(total_amount - discount) +
    tax_on_discounted_subtotal` -- discount applied before tax, matching
    common real-world invoicing order. 100 - 20 = 80 discounted subtotal;
    10% tax on 80 = 8.00; grand_total = 88.00."""
    from app.schemas.billing import InvoiceResponse

    billing_engine.add_invoice_item(
        db,
        invoice_id=open_invoice.id,
        source_type="consultation",
        source_id=ended_consultation.id,
        description="Consultation fee",
        amount=Decimal("100.00"),
        actor_user_id=staff_user.id,
    )
    updated = billing_engine.apply_invoice_adjustments(
        db,
        invoice_id=open_invoice.id,
        tax_rate_percent=Decimal("10.00"),
        discount_amount=Decimal("20.00"),
        actor_user_id=staff_user.id,
    )
    summary = InvoiceResponse.model_validate(updated)
    assert summary.tax_amount == Decimal("8.00")
    assert summary.grand_total == Decimal("88.00")


def test_invoice_response_grand_total_defaults_to_total_amount_with_no_adjustments(open_invoice):
    """An invoice that never calls `apply_invoice_adjustments` (every
    pre-existing invoice, and any invoice whose adjustments are never set)
    has `grand_total == total_amount` exactly -- this HMS Project Completion
    Prompt addition must be a strict no-op by default."""
    from app.schemas.billing import InvoiceResponse

    summary = InvoiceResponse.model_validate(open_invoice)
    assert summary.tax_amount == Decimal("0.00")
    assert summary.grand_total == summary.total_amount
