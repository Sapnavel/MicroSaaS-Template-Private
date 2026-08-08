from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AppointmentCreate(BaseModel):
    patient_id: UUID
    doctor_id: UUID
    room_id: UUID
    start_time: datetime
    duration_minutes: int = Field(gt=0, le=240)
    equipment_ids: list[UUID] = Field(default_factory=list)


class AppointmentResponse(BaseModel):
    id: UUID
    branch_id: UUID
    patient_id: UUID
    doctor_id: UUID
    room_id: UUID
    status: str
    is_emergency: bool
    triage_level: int | None = None
    series_id: UUID | None = None
    reschedule_of_id: UUID | None = None

    model_config = {"from_attributes": True}


class AppointmentRescheduleRequest(BaseModel):
    """PATCH /api/v1/appointments/{id}/reschedule body. HMS Project
    Completion Prompt gap ("rescheduling"). `patient_id` is deliberately NOT
    a field here -- the patient is derived from the appointment being
    rescheduled (see `scheduling_engine.reschedule_appointment`'s
    docstring), never caller-supplied."""

    doctor_id: UUID
    room_id: UUID
    start_time: datetime
    duration_minutes: int = Field(gt=0, le=240)
    equipment_ids: list[UUID] = Field(default_factory=list)


class RecurringAppointmentCreate(BaseModel):
    """POST /api/v1/appointments/recurring body. `occurrences` is capped at
    52 (matches `appointment_series.occurrences`'s own CHECK constraint,
    database/schema.sql) -- a booking tool, not an arbitrary-length
    scheduling batch job."""

    patient_id: UUID
    doctor_id: UUID
    room_id: UUID
    start_time: datetime
    duration_minutes: int = Field(gt=0, le=240)
    frequency: Literal["daily", "weekly", "biweekly"]
    occurrences: int = Field(ge=2, le=52)


class RecurringAppointmentFailure(BaseModel):
    start_time: datetime
    reason: str


class RecurringAppointmentResponse(BaseModel):
    series_id: UUID
    booked: list[AppointmentResponse]
    failed: list[RecurringAppointmentFailure]


class WaitlistJoinCreate(BaseModel):
    patient_id: UUID
    doctor_id: UUID
    requested_date: date


class WaitlistEntryResponse(BaseModel):
    id: UUID
    branch_id: UUID
    patient_id: UUID
    doctor_id: UUID
    requested_date: date
    status: str
    resolved_appointment_id: UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class WaitlistFulfillRequest(BaseModel):
    appointment_id: UUID


class AppointmentListItemResponse(BaseModel):
    """GET /api/v1/appointments response line (frontend UUID-to-dropdown
    conversion follow-up, backend phase) -- feeds the queue check-in
    dropdown (a front_desk/nurse/doctor picks an existing appointment to
    check a patient into the queue for, rather than typing a raw
    appointment_id -- see services/queue_service.check_in's
    appointment-linked path). Built by hand in
    services/appointment_service.py from an `Appointment` joined to
    `Patient` (not plain `from_attributes` off an `Appointment` row alone --
    `patient_name` lives on `Patient`, not `Appointment`)."""

    id: UUID
    patient_id: UUID
    patient_name: str
    doctor_id: UUID
    room_id: UUID
    status: str
    is_emergency: bool
    triage_level: int | None = None


class EmergencyBookingCreate(BaseModel):
    patient_id: UUID
    specialty_id: int
    triage_level: int = Field(ge=1, le=2, description="Only levels 1-2 trigger preemption")
    room_type: str = "consultation"
    equipment_ids: list[UUID] = Field(default_factory=list)
    duration_minutes: int = Field(default=20, gt=0, le=180)
