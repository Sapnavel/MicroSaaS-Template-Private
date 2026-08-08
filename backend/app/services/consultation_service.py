"""Clinical Consultation & Smart Prescription Engine business logic.
Routers (routers/consultations.py) stay thin and delegate here -- same
"business logic lives in services/, router is thin" split as
services/patient_service.py and services/scheduling_engine.py.

Design notes (see PRPs/clinical-consultation-prescription-prp.md,
"ENDPOINTS" / "SAFETY ENGINE DESIGN"):

- `Consultation` has no `branch_id` (see models/consultation.py's module
  docstring) -- `authorize()`'s tenant guard is a no-op here by design, same
  reasoning as the Patient module. The real access control is the
  registered ownership policies (`_doctor_reads_own_consultations` /
  `_doctor_writes_own_consultations` in core/security.py), called via
  `authorize(current_user, "consultation", "read"|"write", consultation)`
  before every read/mutation below.
- `current_user.doctor_id` (a `doctors.id`, stashed by `get_current_user`,
  NOT `current_user.id`) is what identifies "which Doctor record is this
  caller" -- see core/security.py's `get_current_user` docstring for why
  comparing `consultation.doctor_id == current_user.id` would be wrong (two
  different id spaces). `start_consultation` below is the one place this
  module does that ownership comparison manually (before a `Consultation`
  row exists to call `authorize()` against); every other mutation/read goes
  through the registered `authorize()` policies instead, which already
  handle a doctor-role caller with `doctor_id is None` safely (the policy's
  equality check just never matches, yielding a generic 403) -- only
  `start_consultation` gets the more specific, clearer 403 message for that
  data-consistency problem, since the PRP calls that case out explicitly.
- Every function that can fail in an API-visible way raises `HTTPException`
  directly (same convention as `patient_service.py`), EXCEPT the three
  prescription-safety outcomes that need response bodies with extra
  top-level keys FastAPI's `HTTPException(detail=...)` can't produce
  (`blocking_findings` / `override_required_findings`) -- those raise the
  module-local `PrescriptionBlocked` / `PrescriptionOverrideRequired`
  exceptions instead, exactly the same architectural split
  `patient_service.DeterministicMatchConflict` uses for its 409 body.
- Never let raw `notes`/`symptoms`/allergy `reaction` text (all PHI-adjacent
  free text) reach a `logger.` call anywhere in this module.
"""

import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import authorize
from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit import record_audit_event
from app.models.consultation import Consultation, Diagnosis, Drug, PatientAllergy, Prescription, PrescriptionItem
from app.models.patient import Patient
from app.models.resource import Doctor
from app.models.user import User, UserRole
from app.schemas.consultation import (
    AllergyCreate,
    ConsultationCompleteRequest,
    ConsultationCreate,
    ConsultationListItemResponse,
    ConsultationResponse,
    DiagnosisCreate,
    DiagnosisSummary,
    PrescriptionCreateRequest,
    PrescriptionItemResponse,
    PrescriptionResponse,
)
from app.services import prescription_safety, scheduling_engine
from app.services.prescription_safety import PrescriptionSafetyReport, SafetyFinding, SafetyTier

logger = logging.getLogger(__name__)

# Loose ICD-10/11 SHAPE check only -- one letter, two digits, optional
# ".subcode" of 1-4 alphanumeric characters (e.g. "A00", "S52.5X1A"). This
# is NOT a lookup against a real ICD-10/11 code table: no such reference
# table exists anywhere in this schema, and building one is out of scope
# per the PRP. It exists only to reject obviously-malformed input
# (empty strings, garbage) -- a syntactically-valid-looking but entirely
# made-up code will pass this check, and that is an accepted limitation.
_ICD_CODE_PATTERN = re.compile(r"^[A-Za-z]\d{2}(\.[A-Za-z0-9]{1,4})?$")


class PrescriptionBlocked(Exception):
    """Raised by `create_prescription` when the safety check finds at least
    one BLOCK-tier finding (a contraindicated interaction or a severe
    allergy). No override is possible at any input -- nothing is persisted.
    Carries the blocking findings so `routers/consultations.py` can build
    the 422 response's `blocking_findings` key -- a plain `HTTPException`
    can't produce that shape (FastAPI always wraps `.detail` as
    `{"detail": ...}`), same reasoning as `patient_service.
    DeterministicMatchConflict`."""

    def __init__(self, findings: list[SafetyFinding]) -> None:
        self.findings = findings
        super().__init__("prescription safety check found blocking findings")


class PrescriptionOverrideRequired(Exception):
    """Raised by `create_prescription` when there is at least one
    OVERRIDE_REQUIRED finding and the caller did not pass
    `override=true` + a non-empty `override_reason`. Carries the findings
    needing acknowledgement for the 409 response's
    `override_required_findings` key -- same reasoning as
    `PrescriptionBlocked` above."""

    def __init__(self, findings: list[SafetyFinding]) -> None:
        self.findings = findings
        super().__init__("prescription safety check requires override acknowledgement")


# --- Consultations ------------------------------------------------------


def _get_consultation_or_404(db: Session, consultation_id: uuid.UUID) -> Consultation:
    consultation = db.get(Consultation, consultation_id)
    if consultation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "consultation not found")
    return consultation


def start_consultation(db: Session, current_user: User, payload: ConsultationCreate) -> Consultation:
    """POST /api/v1/consultations. `doctor_id`/`patient_id` on the created
    row come from the referenced `Appointment`, never from client input or
    `current_user` directly (a doctor could otherwise "start" a consultation
    naming themselves as doctor on someone else's appointment)."""
    appointment = db.get(Appointment, payload.appointment_id)
    if appointment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "appointment not found")

    if current_user.role == UserRole.doctor:
        # `get_current_user` stashes `doctor_id` (a `doctors.id`) as `None`
        # when a doctor-role user has no matching `Doctor` row -- a
        # data-consistency problem, not a normal "wrong doctor" case. Treat
        # it as an explicit 403 rather than letting `None != appointment.
        # doctor_id` fall through to the same message as a genuine ownership
        # mismatch below (per the Phase 2 brief).
        if current_user.doctor_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "your account has role=doctor but no matching Doctor record; contact an administrator",
            )
        if appointment.doctor_id != current_user.doctor_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "doctors may only start a consultation for their own appointments",
            )
    # system_admin bypasses the ownership check (no ownership concept for
    # an administrator acting across doctors), matching this module's other
    # write paths.

    if appointment.status != AppointmentStatus.in_progress:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"appointment must be in_progress to start a consultation (current status: {appointment.status.value})",
        )

    consultation = Consultation(
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        patient_id=appointment.patient_id,
        symptoms=payload.symptoms,
    )
    db.add(consultation)
    try:
        db.commit()
    except IntegrityError as exc:
        # `appointment_id` is UNIQUE on `consultations` -- a second attempt
        # to start a consultation for the same appointment hits that
        # constraint. Turn it into a clean 409 instead of a raw
        # constraint-violation 500.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a consultation already exists for appointment {appointment.id}",
        ) from exc
    db.refresh(consultation)
    return consultation


def get_consultation(db: Session, current_user: User, consultation_id: uuid.UUID) -> Consultation:
    consultation = _get_consultation_or_404(db, consultation_id)
    authorize(current_user, "consultation", "read", consultation)
    return consultation


def list_consultations_for_patient(
    db: Session, current_user: User, patient_id: uuid.UUID
) -> list[ConsultationListItemResponse]:
    """GET /api/v1/consultations?patient_id= (frontend UUID-to-dropdown
    conversion follow-up, backend phase). Router gates to doctor/system_admin
    only (routers/consultations.py, matching LabOrderPage's `/lab/orders/new`
    route gate).

    For a `doctor` caller, this returns ONLY the caller's own consultations
    for this patient -- consistent with the ownership-based ABAC policy every
    other consultation read/write in this module enforces
    (`_doctor_reads_own_consultations` in core/security.py), applied here as
    a query-level filter rather than one `authorize()` call per row (which
    would mean silently dropping some rows on a 403 instead of raising it --
    a query-level filter is the cleaner way to express "only mine" for a list
    endpoint). A doctor-role caller with no wired `Doctor` record
    (`current_user.doctor_id is None`, see `core/security.py`'s
    `get_current_user` docstring) gets an empty list rather than an error --
    same "the equality check just never matches" behavior the registered
    ownership policy already gives every other doctor-scoped read/write in
    this module.

    `system_admin` sees every consultation for the patient, no ownership
    filter -- an admin auditing a patient's full clinical history is exactly
    what this role exists for (same `_admin_bypass` reasoning every other
    module's ABAC policy block documents).
    """
    query = (
        select(Consultation.id, Consultation.appointment_id, Consultation.started_at, User.full_name)
        .join(Doctor, Doctor.id == Consultation.doctor_id)
        .join(User, User.id == Doctor.user_id)
        .where(Consultation.patient_id == patient_id)
        .order_by(Consultation.started_at.desc())
    )
    return _run_consultation_list_query(db, current_user, query)


def list_consultations_for_appointment(
    db: Session, current_user: User, appointment_id: uuid.UUID
) -> list[ConsultationListItemResponse]:
    """GET /api/v1/consultations?appointment_id= -- see routers/
    consultations.py's docstring on why this exists alongside `?patient_id=`.
    Same ownership filtering as `list_consultations_for_patient` (a doctor
    only ever sees their own); `Consultation.appointment_id` is UNIQUE so
    this returns at most one row."""
    query = (
        select(Consultation.id, Consultation.appointment_id, Consultation.started_at, User.full_name)
        .join(Doctor, Doctor.id == Consultation.doctor_id)
        .join(User, User.id == Doctor.user_id)
        .where(Consultation.appointment_id == appointment_id)
        .order_by(Consultation.started_at.desc())
    )
    return _run_consultation_list_query(db, current_user, query)


def _run_consultation_list_query(db: Session, current_user: User, query: Select) -> list[ConsultationListItemResponse]:
    if current_user.role == UserRole.doctor:
        caller_doctor_id = getattr(current_user, "doctor_id", None)
        if caller_doctor_id is None:
            return []
        query = query.where(Consultation.doctor_id == caller_doctor_id)
    # else: system_admin (the only other role require_role permits on either
    # list endpoint) -- no ownership filter, see docstring.

    rows = db.execute(query).all()
    return [
        ConsultationListItemResponse(id=row[0], appointment_id=row[1], created_at=row[2], doctor_name=row[3])
        for row in rows
    ]


def _time_range_upper_as_datetime(value: object) -> datetime | None:
    """Extract a `TSTZRANGE` upper bound (already-loaded psycopg2
    `Range.upper`) as a `datetime` -- mirrors `ward_service.py`'s
    `_range_bound_as_datetime` exactly (same driver behavior: usually a
    `datetime` already, occasionally a string, per that function's
    docstring), duplicated here rather than imported since it's a one-line,
    module-local concern in both places, not a shared abstraction."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"unexpected range bound type: {type(value)!r}")


def complete_consultation(
    db: Session, current_user: User, consultation_id: uuid.UUID, payload: ConsultationCompleteRequest
) -> Consultation:
    consultation = _get_consultation_or_404(db, consultation_id)
    authorize(current_user, "consultation", "write", consultation)

    if consultation.ended_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this consultation has already been completed")

    consultation.ended_at = datetime.now(timezone.utc)
    if payload.notes is not None:
        consultation.notes = payload.notes
    db.add(consultation)
    db.commit()
    db.refresh(consultation)

    # Whole-system review finding: `scheduling_engine.
    # recalculate_downstream_wait_times` existed with zero call sites --
    # the consumer side of the queue-wait-time pipeline
    # (workers/queue_wait_time_consumer.py) was real and would have worked,
    # but nothing ever produced its input event. This is the producer:
    # completing a consultation is exactly "the doctor is now free", so if
    # they ran past this appointment's own scheduled end, every later
    # booked appointment for this doctor today needs its estimate shifted.
    # Runs after the commit above (best-effort event publish, not part of
    # the consultation-completion transaction itself -- same "read-then-act
    # post-commit" shape `patient_service.create_patient`'s duplicate scan
    # already uses).
    appointment = db.get(Appointment, consultation.appointment_id)
    if appointment is not None:
        scheduled_end = _time_range_upper_as_datetime(appointment.time_range.upper)
        if scheduled_end is not None and consultation.ended_at > scheduled_end:
            scheduling_engine.recalculate_downstream_wait_times(db, appointment.doctor_id, consultation.ended_at)

    return consultation


def _reject_if_completed(consultation: Consultation) -> None:
    """Shared guard for every consultation mutation except `complete_consultation`
    itself. Without this, a consultation stays fully writable after
    `ended_at` is set -- `complete_consultation` only guarded against being
    called twice, not against `add_diagnosis`/`create_prescription` being
    called afterward at all (REVIEW-AGENT finding M1,
    PRPs/clinical-consultation-prescription-prp.md Phase 3)."""
    if consultation.ended_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this consultation has already been completed and can no longer be modified",
        )


def add_diagnosis(
    db: Session, current_user: User, consultation_id: uuid.UUID, payload: DiagnosisCreate
) -> Diagnosis:
    consultation = _get_consultation_or_404(db, consultation_id)
    authorize(current_user, "consultation", "write", consultation)
    _reject_if_completed(consultation)

    if not _ICD_CODE_PATTERN.match(payload.icd_code.strip()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "icd_code must roughly match ICD-10/11 shape (one letter, two digits, optional "
            ".subcode of 1-4 alphanumeric characters) -- this is a format check only, not a "
            "lookup against a real ICD-10/11 reference table",
        )

    if payload.is_primary:
        existing_primary = db.execute(
            select(Diagnosis.id).where(
                Diagnosis.consultation_id == consultation_id, Diagnosis.is_primary.is_(True)
            )
        ).scalar_one_or_none()
        if existing_primary is not None:
            # Deliberately NOT auto-demoting the existing primary diagnosis
            # -- per the PRP, the caller must fix its own request (e.g.
            # PATCH the old one to is_primary=false first, if such an
            # endpoint existed) rather than this silently rewriting clinical
            # data on their behalf.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "this consultation already has a primary diagnosis; demote it explicitly "
                "before adding another is_primary=true diagnosis",
            )

    diagnosis = Diagnosis(
        consultation_id=consultation_id,
        icd_code=payload.icd_code,
        description=payload.description,
        is_primary=payload.is_primary,
    )
    db.add(diagnosis)
    db.commit()
    db.refresh(diagnosis)
    return diagnosis


# --- Allergies ------------------------------------------------------------


def _get_patient_or_404(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found")
    return patient


def list_allergies(db: Session, patient_id: uuid.UUID) -> list[PatientAllergy]:
    _get_patient_or_404(db, patient_id)
    return list(
        db.execute(select(PatientAllergy).where(PatientAllergy.patient_id == patient_id)).scalars().all()
    )


def add_allergy(db: Session, current_user: User, patient_id: uuid.UUID, payload: AllergyCreate) -> PatientAllergy:
    """POST /api/v1/patients/{patient_id}/allergies. No ownership
    restriction beyond role (any doctor/nurse/system_admin may record an
    allergy for any patient) -- REVIEW-AGENT assessed this as the correct
    call, not a gap: allergy history is intake data with no natural
    "this patient is mine yet" relationship the way a Consultation has, and
    restricting it would break ordinary walk-in/triage workflow. What WAS a
    real gap (REVIEW-AGENT finding M2): this table feeds the safety engine's
    BLOCK/OVERRIDE tier directly, yet had zero audit trail -- a bad or
    malicious entry can wrongly block a needed drug or clear a dangerous
    one, unlike prescription overrides which are already fully logged. Audit
    the write without adding the (rejected) ownership restriction."""
    _get_patient_or_404(db, patient_id)
    allergy = PatientAllergy(
        patient_id=patient_id,
        substance=payload.substance,
        severity=payload.severity,
        reaction=payload.reaction,
    )
    db.add(allergy)
    db.flush()  # assign allergy.id for the audit resource_id
    # Whole-system review finding H2: metadata used to include the raw
    # `substance` (and implicitly identified the specific allergy) in
    # `audit_logs.metadata`, a plain unencrypted JSONB column -- breaking
    # the exact PHI-duplication discipline lab_service.py documents and
    # follows ("never the value itself, only whether it happened"). The
    # substance/reaction text lives in `patient_allergies` (its own,
    # purpose-built table) and is reachable from `resource_id` by anyone
    # with legitimate access to review this audit entry; `severity` alone
    # (a coarse mild/moderate/severe tier, not the allergy itself) is kept
    # since it's useful to triage which audit entries need urgent review
    # without duplicating the PHI-identifying substance/reaction text.
    record_audit_event(
        db,
        branch_id=None,  # patients/allergies are not branch-scoped, see models/patient.py
        actor_user_id=current_user.id,
        action="patient_allergy.recorded",
        resource_type="patient_allergy",
        resource_id=str(allergy.id),
        metadata={"patient_id": str(patient_id), "severity": payload.severity},
    )
    db.commit()
    db.refresh(allergy)
    return allergy


# --- Prescriptions ----------------------------------------------------------


def _finding_to_audit_dict(finding: SafetyFinding) -> dict[str, Any]:
    """Plain-JSON-serializable shape of a `SafetyFinding` for the audit
    log's `metadata` (a JSON column) -- `SafetyFinding` is a frozen
    dataclass holding `uuid.UUID`s and a `SafetyTier` enum, neither of which
    `json.dumps` handles without help (record_audit_event's payload hashing
    does `json.dumps(..., default=str)`, which would stringify these
    reasonably, but building the dict explicitly here keeps the exact shape
    intentional rather than relying on `default=str`'s fallback)."""
    return {
        "source": finding.source,
        "severity": finding.severity,
        "tier": finding.tier.value,
        "message": finding.message,
        "drug_ids": [str(drug_id) for drug_id in finding.drug_ids],
    }


def create_prescription(
    db: Session,
    current_user: User,
    consultation_id: uuid.UUID,
    payload: PrescriptionCreateRequest,
) -> tuple[Prescription, list[PrescriptionItem], PrescriptionSafetyReport]:
    """POST /api/v1/consultations/{id}/prescriptions -- the safety-gated
    flow from the PRP's "SAFETY ENGINE DESIGN" / endpoint flow section.
    Every code path that ends in a persisted `Prescription`/
    `PrescriptionItem` in this module goes through this function, and this
    function always calls `prescription_safety.evaluate_prescription_safety`
    first -- there is no bypass path.
    """
    consultation = _get_consultation_or_404(db, consultation_id)
    authorize(current_user, "consultation", "write", consultation)
    _reject_if_completed(consultation)

    drug_ids = [item.drug_id for item in payload.items]

    existing_drug_ids = set(db.execute(select(Drug.id).where(Drug.id.in_(drug_ids))).scalars().all())
    missing_drug_ids = [drug_id for drug_id in dict.fromkeys(drug_ids) if drug_id not in existing_drug_ids]
    if missing_drug_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown drug_id(s): {', '.join(str(drug_id) for drug_id in missing_drug_ids)}",
        )

    report = prescription_safety.evaluate_prescription_safety(db, consultation.patient_id, drug_ids)

    blocking_findings = [finding for finding in report.all_findings if finding.tier == SafetyTier.BLOCK]
    if blocking_findings:
        raise PrescriptionBlocked(blocking_findings)

    override_required_findings = [
        finding for finding in report.all_findings if finding.tier == SafetyTier.OVERRIDE_REQUIRED
    ]
    did_override = False
    if override_required_findings:
        if not payload.override:
            raise PrescriptionOverrideRequired(override_required_findings)
        if not payload.override_reason or not payload.override_reason.strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "override_reason is required (non-empty) when overriding a safety warning "
                "that requires acknowledgement",
            )
        did_override = True
    # else: payload.override may still be True here (a redundant flag when
    # there are no OVERRIDE_REQUIRED findings at all) -- per the PRP, that's
    # not an error and nothing was actually overridden, so `did_override`
    # correctly stays False and no audit log is written below.

    prescription = Prescription(
        consultation_id=consultation.id,
        patient_id=consultation.patient_id,
        status="finalized",
    )
    db.add(prescription)
    db.flush()  # assign prescription.id for the PrescriptionItem FKs / audit resource_id

    items: list[PrescriptionItem] = []
    for item_payload in payload.items:
        item = PrescriptionItem(
            prescription_id=prescription.id,
            drug_id=item_payload.drug_id,
            dosage=item_payload.dosage,
            frequency=item_payload.frequency,
            duration_days=item_payload.duration_days,
        )
        db.add(item)
        items.append(item)

    if did_override:
        logger.info(
            "prescription %s created with safety override by user=%s",
            prescription.id,
            current_user.id,
        )
        record_audit_event(
            db,
            branch_id=None,  # consultations/prescriptions are not branch-scoped, see module docstring
            actor_user_id=current_user.id,
            action="prescription.override_safety_warning",
            resource_type="prescription",
            resource_id=str(prescription.id),
            metadata={
                "override_reason": payload.override_reason,
                "overridden_findings": [_finding_to_audit_dict(f) for f in override_required_findings],
            },
        )

    db.commit()
    db.refresh(prescription)
    for item in items:
        db.refresh(item)

    return prescription, items, report


def _prescriptions_with_items(
    db: Session, consultation_id: uuid.UUID
) -> list[tuple[Prescription, list[PrescriptionItem]]]:
    """Fetch every `Prescription` for a consultation plus its
    `PrescriptionItem`s, batched (one query for prescriptions, one for all
    their items) rather than N+1 -- neither model has an ORM relationship()
    defined between them (see models/consultation.py), so this is done
    explicitly instead of via `joinedload`."""
    prescriptions = list(
        db.execute(
            select(Prescription)
            .where(Prescription.consultation_id == consultation_id)
            .order_by(Prescription.created_at)
        )
        .scalars()
        .all()
    )
    if not prescriptions:
        return []

    prescription_ids = [p.id for p in prescriptions]
    items = list(
        db.execute(select(PrescriptionItem).where(PrescriptionItem.prescription_id.in_(prescription_ids)))
        .scalars()
        .all()
    )
    items_by_prescription: dict[uuid.UUID, list[PrescriptionItem]] = defaultdict(list)
    for item in items:
        items_by_prescription[item.prescription_id].append(item)

    return [(p, items_by_prescription.get(p.id, [])) for p in prescriptions]


def list_prescriptions(
    db: Session, current_user: User, consultation_id: uuid.UUID
) -> list[tuple[Prescription, list[PrescriptionItem]]]:
    consultation = _get_consultation_or_404(db, consultation_id)
    authorize(current_user, "consultation", "read", consultation)
    return _prescriptions_with_items(db, consultation_id)


# --- Response shaping --------------------------------------------------------


def build_consultation_response(db: Session, consultation: Consultation) -> ConsultationResponse:
    """The single place a `Consultation` ORM row becomes a
    `ConsultationResponse` (used by the create/get/complete endpoints) --
    same "one function shapes the response" discipline as
    `patient_service.shape_patient_response`. Queries diagnoses and
    prescriptions+items explicitly since neither is an ORM relationship on
    `Consultation` (see models/consultation.py)."""
    diagnoses = list(
        db.execute(select(Diagnosis).where(Diagnosis.consultation_id == consultation.id).order_by(Diagnosis.id))
        .scalars()
        .all()
    )
    prescriptions_with_items = _prescriptions_with_items(db, consultation.id)

    return ConsultationResponse(
        id=consultation.id,
        appointment_id=consultation.appointment_id,
        doctor_id=consultation.doctor_id,
        patient_id=consultation.patient_id,
        symptoms=consultation.symptoms,
        notes=consultation.notes,
        started_at=consultation.started_at,
        ended_at=consultation.ended_at,
        diagnoses=[DiagnosisSummary.model_validate(d) for d in diagnoses],
        prescriptions=[
            PrescriptionResponse(
                id=p.id,
                status=p.status,
                created_at=p.created_at,
                items=[PrescriptionItemResponse.model_validate(i) for i in items],
            )
            for p, items in prescriptions_with_items
        ],
    )
