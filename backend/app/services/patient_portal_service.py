"""Patient Self-Service Portal business logic (routers/patient_portal.py,
`/api/v1/me/*`). HMS Project Completion Prompt gap: the `patient` role
(models/user.py `UserRole.patient`) could register and log in, but had no
backend route anywhere scoped to "my own" data -- `Patient.user_id` (the
column linking a portal login to its canonical clinical record) existed but
was written and read nowhere.

Ownership model, deliberately different from every staff-facing module:
every function here derives `patient_id` from `current_user` (via
`_get_linked_patient`), NEVER from a client-supplied id -- there is no
"which patient am I acting on" parameter for a patient caller to get wrong
or spoof, unlike `doctor`/`nurse`/`front_desk` reads which take an explicit
`patient_id`/`consultation_id`/etc. and rely on `authorize()` / ownership
policies to reject a mismatched one. This is also why most reads below do
NOT call `core.security.authorize()`: that function's tenant guard checks a
resource's `branch_id` against the caller's own `branch_id`, but patients
have no `branch_id` (`register_patient` sets it to `None`, by design -- a
patient's appointments/invoices/lab orders can legitimately span many
branches). Routing patient-portal reads through `authorize()` would 403
every single one. Ownership is enforced structurally instead: every query
below is pre-filtered to rows whose `patient_id` equals the caller's own
linked patient, so there is no row to leak in the first place.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import InsuranceClaim, Invoice, InvoiceItem
from app.models.consultation import Consultation
from app.models.lab import LabOrder
from app.models.patient import Patient
from app.models.resource import Doctor, Room
from app.models.user import User
from app.schemas.appointment import AppointmentResponse
from app.schemas.billing import InsuranceClaimResponse, InvoiceDetailResponse, InvoiceItemResponse, InvoiceResponse
from app.schemas.consultation import ConsultationListItemResponse, ConsultationResponse
from app.schemas.lab import LabOrderResponse
from app.schemas.patient import PatientCreate, PatientFullRecord
from app.schemas.patient_portal import MyAppointmentCreate, MyDoctorListItem, MyPatientCreate
from app.services import consultation_service, lab_service, patient_service, patient_timeline_service
from app.services.patient_timeline_service import TimelineEvent
from app.services.scheduling_engine import (
    BookingConflictError,
    BookingRequest,
    ResourceBusyError,
    book_appointment,
    cancel_appointment,
)

_MAX_ROOM_ATTEMPTS = 10


def _get_linked_patient(db: Session, current_user: User) -> Patient:
    patient = db.execute(select(Patient).where(Patient.user_id == current_user.id)).scalar_one_or_none()
    if patient is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no patient profile is linked to this account yet; complete your profile via POST /api/v1/me/patient",
        )
    return patient


def get_my_patient_record(db: Session, current_user: User) -> PatientFullRecord:
    """GET /api/v1/me/patient. No `authorize()` call: a patient reading their
    own linked record is not gated by the staff-facing ownership policies in
    core/security.py (those only cover front_desk/doctor/nurse/system_admin
    -- see that module's registry), and structurally there is nothing to gate
    since `_get_linked_patient` can only ever resolve to the caller's own
    row."""
    patient = _get_linked_patient(db, current_user)
    return PatientFullRecord.model_validate(patient)


def get_my_timeline(db: Session, current_user: User) -> list[TimelineEvent]:
    """GET /api/v1/me/timeline. HMS Project Completion Prompt gap ("Patient
    medical timeline"), patient-facing counterpart to
    `routers/patients.py`'s staff `GET /{id}/timeline` -- same underlying
    `patient_timeline_service.get_patient_timeline`, no `authorize()` call
    needed for the same structural reason `get_my_patient_record` above
    documents (the patient_id comes from the caller's own linked record,
    never a client-supplied one)."""
    patient = _get_linked_patient(db, current_user)
    return patient_timeline_service.get_patient_timeline(db, patient.id)


def create_my_patient_record(db: Session, current_user: User, payload: MyPatientCreate) -> PatientFullRecord:
    """POST /api/v1/me/patient. 409 if this login is already linked to a
    patient record -- one login maps to at most one canonical patient
    (`Patient.user_id` is UNIQUE; see that column's docstring)."""
    already_linked = db.execute(
        select(Patient.id).where(Patient.user_id == current_user.id)
    ).scalar_one_or_none()
    if already_linked is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a patient profile is already linked to this account")

    create_payload = PatientCreate(
        full_name=payload.full_name,
        dob=payload.dob,
        sex=payload.sex,
        phone=payload.phone,
        national_id=payload.national_id,
        address=payload.address,
        allergies_note=payload.allergies_note,
        force=False,
    )
    patient = patient_service.create_patient(db, current_user, create_payload, link_user_id=current_user.id)
    return PatientFullRecord.model_validate(patient)


def search_doctors_for_booking(
    db: Session, branch_id: uuid.UUID | None, specialty_id: int | None
) -> list[MyDoctorListItem]:
    """GET /api/v1/me/doctors?branch_id=&specialty_id=. Deliberately does
    NOT reuse `directory_service.list_doctors`: that function calls
    `authorize(current_user, "directory", "read", _BranchScoped(branch_id))`,
    which -- per this module's docstring -- would 403 every patient caller
    (no `branch_id` to satisfy the tenant guard). Doctor name/specialty/
    branch is public-enough reference data for a patient choosing where to
    book (no PHI, nothing patient-specific), so this query is intentionally
    unscoped: any authenticated patient may browse any branch's doctors,
    `branch_id`/`specialty_id` are optional narrowing filters here (unlike
    the staff endpoint, where `branch_id` is required)."""
    query = (
        select(Doctor.id, User.full_name, Doctor.specialty_id, Doctor.branch_id)
        .join(User, User.id == Doctor.user_id)
        .where(Doctor.is_active.is_(True))
    )
    if branch_id is not None:
        query = query.where(Doctor.branch_id == branch_id)
    if specialty_id is not None:
        query = query.where(Doctor.specialty_id == specialty_id)
    query = query.order_by(User.full_name)

    rows = db.execute(query).all()
    return [MyDoctorListItem(id=row[0], full_name=row[1], specialty_id=row[2], branch_id=row[3]) for row in rows]


def list_my_appointments(db: Session, current_user: User) -> list[AppointmentResponse]:
    """GET /api/v1/me/appointments -- every appointment for the caller's own
    linked patient, across every branch, most recent first."""
    patient = _get_linked_patient(db, current_user)
    appointments = (
        db.execute(
            select(Appointment)
            .where(Appointment.patient_id == patient.id)
            .order_by(Appointment.time_range.desc())
        )
        .scalars()
        .all()
    )
    return [AppointmentResponse.model_validate(a) for a in appointments]


def book_my_appointment(db: Session, current_user: User, payload: MyAppointmentCreate) -> AppointmentResponse:
    """POST /api/v1/me/appointments. Books the caller's own linked patient
    into the requested doctor's schedule, auto-selecting the first
    available `room_type='consultation'` room in the chosen branch --
    unlike staff booking (`POST /api/v1/appointments`), a patient caller has
    no concept of which physical rooms exist to choose from.

    Retries across every active consultation room in the branch (bounded by
    `_MAX_ROOM_ATTEMPTS`) so that one busy room doesn't fail the whole
    booking when a different room in the same branch is free at this exact
    slot -- `book_appointment` itself still does the real, authoritative
    conflict check per attempt (Redis lock + Postgres EXCLUDE constraint,
    see scheduling_engine.py)."""
    patient = _get_linked_patient(db, current_user)

    doctor = db.get(Doctor, payload.doctor_id)
    if doctor is None or doctor.branch_id != payload.branch_id or not doctor.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doctor not found at this branch")

    rooms = (
        db.execute(
            select(Room.id)
            .where(Room.branch_id == payload.branch_id, Room.room_type == "consultation", Room.is_active.is_(True))
            .order_by(Room.name)
            .limit(_MAX_ROOM_ATTEMPTS)
        )
        .scalars()
        .all()
    )
    if not rooms:
        raise HTTPException(status.HTTP_409_CONFLICT, "no consultation rooms are configured at this branch")

    last_error: Exception | None = None
    for room_id in rooms:
        request = BookingRequest(
            branch_id=payload.branch_id,
            patient_id=patient.id,
            doctor_id=payload.doctor_id,
            room_id=room_id,
            start_time=payload.start_time,
            duration_minutes=payload.duration_minutes,
        )
        try:
            appointment = book_appointment(db, request)
            return AppointmentResponse.model_validate(appointment)
        except (BookingConflictError, ResourceBusyError) as exc:
            last_error = exc
            continue

    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"this doctor has no free room at the requested time: {last_error}",
    )


def cancel_my_appointment(db: Session, current_user: User, appointment_id: uuid.UUID, reason: str) -> None:
    """DELETE /api/v1/me/appointments/{id}. Ownership is a direct
    `appointment.patient_id == my linked patient.id` comparison, not
    `authorize()` -- same reasoning as every other function in this module
    (see module docstring)."""
    patient = _get_linked_patient(db, current_user)
    appointment = db.get(Appointment, appointment_id)
    if appointment is None or appointment.patient_id != patient.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "appointment not found")
    if appointment.status in (AppointmentStatus.completed, AppointmentStatus.cancelled):
        raise HTTPException(status.HTTP_409_CONFLICT, f"appointment is already {appointment.status.value}")
    cancel_appointment(db, appointment, reason)


def list_my_consultations(db: Session, current_user: User) -> list[ConsultationListItemResponse]:
    """GET /api/v1/me/consultations. Reuses
    `consultation_service.list_consultations_for_patient` as-is: that
    function only special-cases `doctor`-role callers (its ownership
    filter); every other role, `patient` included, gets every matching row
    unfiltered -- exactly right here since the `patient_id` passed in is
    already scoped to the caller's own linked patient, never client-supplied."""
    patient = _get_linked_patient(db, current_user)
    return consultation_service.list_consultations_for_patient(db, current_user, patient.id)


def get_my_consultation(db: Session, current_user: User, consultation_id: uuid.UUID) -> ConsultationResponse:
    """GET /api/v1/me/consultations/{id}. Does NOT call
    `consultation_service.get_consultation` -- that function's
    `authorize(current_user, "consultation", "read", consultation)` has no
    registered policy for role="patient" (core/security.py's registry only
    covers doctor/nurse) and would 403 by default-deny. Ownership is checked
    directly instead, same pattern as `cancel_my_appointment`."""
    patient = _get_linked_patient(db, current_user)
    consultation = db.get(Consultation, consultation_id)
    if consultation is None or consultation.patient_id != patient.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "consultation not found")
    return consultation_service.build_consultation_response(db, consultation)


def list_my_lab_orders(db: Session, current_user: User) -> list[LabOrderResponse]:
    """GET /api/v1/me/lab-orders. Reuses `lab_service.list_orders` +
    `shape_lab_order_responses` as-is: `list_orders` only special-cases
    `doctor`-role callers, and `_shape_one` only special-cases `nurse`-role
    (withholding results) -- a `patient` caller falls through both to "every
    matching row, full result included", exactly right for a patient
    reading their own report."""
    patient = _get_linked_patient(db, current_user)
    orders = lab_service.list_orders(db, current_user, patient_id=patient.id, consultation_id=None, status_filter=None)
    return lab_service.shape_lab_order_responses(db, current_user, orders)


def list_my_invoices(db: Session, current_user: User) -> list[InvoiceResponse]:
    """GET /api/v1/me/bills. Direct query, not `billing_service.
    list_patient_invoices` -- that function requires resolving a single
    `branch_id` and calls `authorize()` against it (see module docstring for
    why that would 403 a patient whose invoices span multiple branches)."""
    patient = _get_linked_patient(db, current_user)
    invoices = (
        db.execute(select(Invoice).where(Invoice.patient_id == patient.id).order_by(Invoice.created_at.desc()))
        .scalars()
        .all()
    )
    return [InvoiceResponse.model_validate(invoice) for invoice in invoices]


def get_my_invoice(db: Session, current_user: User, invoice_id: uuid.UUID) -> InvoiceDetailResponse:
    """GET /api/v1/me/bills/{id}. Ownership check directly against the
    caller's own linked patient, same reasoning as every other single-
    resource read in this module."""
    patient = _get_linked_patient(db, current_user)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.patient_id != patient.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")

    items = db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)).scalars().all()
    claim = db.execute(
        select(InsuranceClaim).where(InsuranceClaim.invoice_id == invoice_id)
    ).scalar_one_or_none()

    return InvoiceDetailResponse(
        invoice=InvoiceResponse.model_validate(invoice),
        items=[InvoiceItemResponse.model_validate(item) for item in items],
        claim=InsuranceClaimResponse.model_validate(claim) if claim is not None else None,
    )
