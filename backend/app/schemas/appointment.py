from datetime import datetime
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

    model_config = {"from_attributes": True}


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
