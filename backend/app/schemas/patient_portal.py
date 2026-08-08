"""Pydantic request schemas for the Patient Self-Service Portal
(routers/patient_portal.py, `/api/v1/me/*`). Response shapes are reused
directly from the staff-facing modules (`schemas.patient.PatientFullRecord`,
`schemas.appointment.AppointmentResponse`, `schemas.consultation.
ConsultationListItemResponse`/`ConsultationResponse`, `schemas.lab.
LabOrderResponse`, `schemas.billing.InvoiceResponse`/`InvoiceDetailResponse`)
-- a patient viewing their own record should see the same shape a doctor/
nurse sees for it, not a parallel schema that could silently drift.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MyPatientCreate(BaseModel):
    """POST /api/v1/me/patient body -- creates and links the caller's own
    canonical `Patient` record. Deliberately narrower than
    `schemas.patient.PatientCreate`: no `force` field (a patient bypassing
    their own deterministic-match conflict is an identity-fraud vector a
    staff member force-creating a walk-in record is not; see
    `patient_portal_service.create_my_patient_record`)."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1)
    dob: date
    sex: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    national_id: str | None = None
    address: str | None = None
    allergies_note: str | None = None


class MyAppointmentCreate(BaseModel):
    """POST /api/v1/me/appointments body. No `patient_id` (always the
    caller's own linked patient) and no `room_id` (auto-assigned to the
    first available consultation room in the chosen branch -- a patient has
    no way to know which physical rooms exist, unlike front_desk/doctor
    booking on `POST /api/v1/appointments`)."""

    model_config = ConfigDict(extra="forbid")

    branch_id: UUID
    doctor_id: UUID
    start_time: datetime
    duration_minutes: int = Field(gt=0, le=240)


class MyDoctorListItem(BaseModel):
    """GET /api/v1/me/doctors response line -- same shape as
    `schemas.directory.DoctorListItem`, duplicated here rather than reused
    directly since this endpoint's underlying query is a deliberately
    unscoped sibling of `directory_service.list_doctors` (see
    `patient_portal_service.search_doctors_for_booking`'s docstring for why
    it cannot reuse that function's `authorize()` call)."""

    id: UUID
    full_name: str
    specialty_id: int
    branch_id: UUID
