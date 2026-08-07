"""Business logic for `GET /api/v1/appointments` (routers/appointments.py,
frontend UUID-to-dropdown conversion follow-up, backend phase) -- lists
appointments for a branch/day to feed the queue check-in dropdown (a
front_desk/nurse/doctor picks an existing appointment to check a patient
into the queue for, rather than typing a raw `appointment_id` -- see
`services/queue_service.check_in`'s appointment-linked path).

This is a pure read with no locking/concurrency concerns, so it lives in its
own thin service module rather than `services/scheduling_engine.py` (that
module is the concurrency-critical booking engine -- Redis lock + Postgres
EXCLUDE constraint layering, see its module docstring -- not a natural home
for a plain list query). Same "read logic gets its own service, separate
from the write engine" split `services/ward_service.py` uses relative to
`services/ward_engine.py`.

TERMINAL-STATUS FILTER (judgment call, per the task brief): excludes
`cancelled`/`completed`/`no_show`/`preempted` -- none of these represent a
patient who could usefully be checked into today's queue (cancelled/no_show/
preempted never happened, or were superseded by a different appointment;
completed has already been seen end-to-end). `booked`/`checked_in`/
`in_progress` are all kept -- `in_progress` is included even though
`queue_service.check_in`'s appointment-linked path doesn't strictly require
any particular status, since a front-desk clerk re-checking a patient into
the queue for an appointment that has already moved to `in_progress` is a
real, if uncommon, workflow this list shouldn't hide.

`department_id` filters by the appointment's doctor's `specialty_id` -- the
exact join path `queue_service.check_in` already uses to derive
`department_id` from an appointment (`Appointment.doctor_id ->
Doctor.specialty_id`).

`date` filters to appointments whose `time_range` overlaps the given
calendar day (UTC), using the same range-overlap (`&&`) operator query style
`ward_service.list_ot_schedules` uses for its `from_time`/`to_time` filter,
rather than trying to cast a `TSTZRANGE` bound to a plain `date` column-side.

Tenant-scoped via `Appointment.branch_id` (a real column) -- `authorize()`
is called against a `_BranchScoped` stand-in, same pattern every other
branch-wide list read in this codebase uses (`queue_service.list_queue`,
`pharmacy_service.get_low_stock`, `ward_service.list_beds`).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import authorize
from app.models.appointment import Appointment, AppointmentStatus
from app.models.patient import Patient
from app.models.resource import Doctor
from app.models.user import User
from app.schemas.appointment import AppointmentListItemResponse

_EXCLUDED_STATUSES = (
    AppointmentStatus.cancelled,
    AppointmentStatus.completed,
    AppointmentStatus.no_show,
    AppointmentStatus.preempted,
)


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard when there
    is no single ORM row to check against (this branch-wide "list today's
    checkable-in appointments" read) -- only `branch_id` matters to the
    guard, same pattern as `queue_service._BranchScoped` and every other
    branch-scoped module's own stand-in."""

    branch_id: uuid.UUID


def list_appointments(
    db: Session,
    current_user: User,
    branch_id: uuid.UUID,
    department_id: int | None,
    on_date: date,
) -> list[AppointmentListItemResponse]:
    """GET /api/v1/appointments?branch_id=&department_id=&date="""
    authorize(current_user, "appointment", "read", _BranchScoped(branch_id=branch_id))

    day_start = datetime.combine(on_date, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    day_range_literal = f"[{day_start.isoformat()},{day_end.isoformat()})"

    query = (
        select(
            Appointment.id,
            Appointment.patient_id,
            Patient.full_name,
            Appointment.doctor_id,
            Appointment.room_id,
            Appointment.status,
            Appointment.is_emergency,
            Appointment.triage_level,
        )
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Appointment.branch_id == branch_id,
            Appointment.status.notin_(_EXCLUDED_STATUSES),
            Appointment.time_range.op("&&")(day_range_literal),
        )
    )
    if department_id is not None:
        query = query.join(Doctor, Doctor.id == Appointment.doctor_id).where(Doctor.specialty_id == department_id)

    query = query.order_by(Appointment.time_range)

    rows = db.execute(query).all()
    return [
        AppointmentListItemResponse(
            id=row[0],
            patient_id=row[1],
            patient_name=row[2],
            doctor_id=row[3],
            room_id=row[4],
            status=row[5].value if isinstance(row[5], AppointmentStatus) else row[5],
            is_emergency=row[6],
            triage_level=row[7],
        )
        for row in rows
    ]
