"""Recurring appointments (HMS Project Completion Prompt, section 3.3:
"Repeat or recurring appointments" -- confirmed entirely missing by the
whole-system audit this session started from).

Each occurrence is booked as its own fully independent
`scheduling_engine.book_appointment` call -- the same atomic Redis-lock +
Postgres-EXCLUDE-constraint path every one-off booking goes through. There
is no separate, weaker "batch insert" code path: an occurrence that
conflicts with something booked in between (a different patient took that
slot, a room got reassigned, etc.) fails on its own and is reported back,
while every occurrence that succeeded stays booked. A recurring request is
therefore best-effort across its occurrences, not all-or-nothing -- forcing
atomicity across N independent future dates would mean one taken slot three
weeks from now cancels a booking happening tomorrow, which is worse for the
patient than partial success.
"""

import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.appointment import AppointmentSeries
from app.models.resource import Doctor, Room
from app.models.user import User
from app.schemas.appointment import RecurringAppointmentCreate, RecurringAppointmentFailure
from app.services.scheduling_engine import (
    BookingConflictError,
    BookingRequest,
    ResourceBusyError,
    book_appointment,
)

_STEP_BY_FREQUENCY = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
}


def _occurrence_start_times(base_start: datetime, frequency: str, occurrences: int) -> list[datetime]:
    step = _STEP_BY_FREQUENCY[frequency]
    return [base_start + step * i for i in range(occurrences)]


def book_recurring_appointments(
    db: Session, current_user: User, branch_id: uuid.UUID, payload: RecurringAppointmentCreate
) -> tuple[AppointmentSeries, list, list[RecurringAppointmentFailure]]:
    """POST /api/v1/appointments/recurring. `branch_id` comes from the
    caller's token (routers/appointments.py resolves it the same way
    `create_appointment` does), not the request body -- same cross-branch
    guard as the one-off booking endpoint."""
    doctor = db.get(Doctor, payload.doctor_id)
    if doctor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doctor not found")
    room = db.get(Room, payload.room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "room not found")
    if doctor.branch_id != branch_id or room.branch_id != branch_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "doctor and room must belong to the caller's own branch",
        )

    series = AppointmentSeries(
        branch_id=branch_id,
        patient_id=payload.patient_id,
        doctor_id=payload.doctor_id,
        frequency=payload.frequency,
        occurrences=payload.occurrences,
        created_by=current_user.id,
    )
    db.add(series)
    db.flush()  # assign series.id -- referenced by each occurrence's series_id FK below

    booked = []
    failed: list[RecurringAppointmentFailure] = []
    for start_time in _occurrence_start_times(payload.start_time, payload.frequency, payload.occurrences):
        request = BookingRequest(
            branch_id=branch_id,
            patient_id=payload.patient_id,
            doctor_id=payload.doctor_id,
            room_id=payload.room_id,
            start_time=start_time,
            duration_minutes=payload.duration_minutes,
            series_id=series.id,
        )
        try:
            booked.append(book_appointment(db, request))
        except (BookingConflictError, ResourceBusyError) as exc:
            failed.append(RecurringAppointmentFailure(start_time=start_time, reason=str(exc)))

    db.commit()
    db.refresh(series)
    return series, booked, failed
