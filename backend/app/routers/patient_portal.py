"""Patient Self-Service Portal: `/api/v1/me/*`. Every endpoint here is
gated to `role=patient` only (`require_role("patient")`) -- this is
deliberately the mirror image of every other router in this codebase, which
gate staff roles IN and leave `patient` out. See
services/patient_portal_service.py's module docstring for the ownership
model (derived from the caller's own linked `Patient` row, never from a
client-supplied id).
"""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.resource import Doctor
from app.models.user import User
from app.schemas.appointment import AppointmentResponse
from app.schemas.billing import InvoiceDetailResponse, InvoiceResponse
from app.schemas.consultation import ConsultationListItemResponse, ConsultationResponse
from app.schemas.lab import LabOrderResponse, LabOrderResponseNoResult
from app.schemas.patient import PatientFullRecord
from app.schemas.patient_portal import MyAppointmentCreate, MyDoctorListItem, MyPatientCreate
from app.services import patient_portal_service
from app.services.patient_timeline_service import TimelineEvent
from app.services.scheduling_engine import BookingConflictError, ResourceBusyError, get_available_slots

router = APIRouter(prefix="/api/v1/me", tags=["patient-portal"])

_PATIENT_ONLY = ("patient",)


@router.get("/patient")
def get_my_patient(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> PatientFullRecord:
    return patient_portal_service.get_my_patient_record(db, current_user)


@router.post("/patient", status_code=status.HTTP_201_CREATED)
def create_my_patient(
    payload: MyPatientCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> PatientFullRecord:
    return patient_portal_service.create_my_patient_record(db, current_user, payload)


@router.get("/timeline")
def get_my_timeline(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[TimelineEvent]:
    """GET /api/v1/me/timeline. HMS Project Completion Prompt gap ("Patient
    medical timeline")."""
    return patient_portal_service.get_my_timeline(db, current_user)


@router.get("/doctors")
def search_doctors(
    branch_id: uuid.UUID | None = None,
    specialty_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[MyDoctorListItem]:
    return patient_portal_service.search_doctors_for_booking(db, branch_id, specialty_id)


@router.get("/doctors/{doctor_id}/available-slots")
def doctor_available_slots(
    doctor_id: uuid.UUID,
    target_date: date = Query(alias="date"),
    duration_minutes: int = Query(default=20, gt=0, le=240),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[datetime]:
    """GET /api/v1/me/doctors/{doctor_id}/available-slots?date=&duration_minutes=
    HMS Project Completion Prompt gap ("available-slot display") -- the
    patient-facing counterpart to `routers/appointments.py`'s
    `available_slots` (staff), same underlying
    `scheduling_engine.get_available_slots`. Any patient may query any
    doctor's availability, same as `search_doctors` above -- doctor
    schedules aren't PHI, and a patient must be able to see a doctor's open
    slots before choosing one to book with."""
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doctor not found")
    return get_available_slots(
        db, doctor_id=doctor_id, target_date=target_date, duration_minutes=duration_minutes
    )


@router.get("/appointments")
def list_my_appointments(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[AppointmentResponse]:
    return patient_portal_service.list_my_appointments(db, current_user)


@router.post("/appointments", status_code=status.HTTP_201_CREATED)
def book_my_appointment(
    payload: MyAppointmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> AppointmentResponse:
    try:
        return patient_portal_service.book_my_appointment(db, current_user, payload)
    except BookingConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc


@router.delete("/appointments/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_my_appointment(
    appointment_id: uuid.UUID,
    reason: str = "cancelled_by_patient",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> None:
    patient_portal_service.cancel_my_appointment(db, current_user, appointment_id, reason)


@router.get("/consultations")
def list_my_consultations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[ConsultationListItemResponse]:
    return patient_portal_service.list_my_consultations(db, current_user)


@router.get("/consultations/{consultation_id}")
def get_my_consultation(
    consultation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> ConsultationResponse:
    return patient_portal_service.get_my_consultation(db, current_user, consultation_id)


@router.get("/lab-orders")
def list_my_lab_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[LabOrderResponse | LabOrderResponseNoResult]:
    return patient_portal_service.list_my_lab_orders(db, current_user)


@router.get("/bills")
def list_my_bills(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> list[InvoiceResponse]:
    return patient_portal_service.list_my_invoices(db, current_user)


@router.get("/bills/{invoice_id}")
def get_my_bill(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_PATIENT_ONLY)),
) -> InvoiceDetailResponse:
    return patient_portal_service.get_my_invoice(db, current_user, invoice_id)
