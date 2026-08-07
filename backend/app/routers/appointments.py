import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import authorize, get_caller_branch_id, get_current_user, require_role
from app.database import get_db
from app.models.resource import Doctor, Room
from app.models.user import User
from app.schemas.appointment import AppointmentCreate, AppointmentListItemResponse, AppointmentResponse
from app.services import appointment_service
from app.services.scheduling_engine import (
    BookingConflictError,
    BookingRequest,
    ResourceBusyError,
    book_appointment,
    cancel_appointment,
)

router = APIRouter(prefix="/api/v1/appointments", tags=["appointments"])

# Matches GET /queue/board's existing read-roles (routers/queue.py) -- this
# endpoint feeds the queue check-in dropdown.
_READ_ROLES = ("front_desk", "nurse", "doctor", "system_admin")


@router.post("", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def create_appointment(
    payload: AppointmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("front_desk", "system_admin", "doctor")),
) -> AppointmentResponse:
    """Whole-system review finding H2: this endpoint used to (a) resolve the
    caller's branch from the live `current_user.branch_id` instead of the
    token-frozen `get_caller_branch_id()` every other branch-scoped module
    uses (see that function's docstring for why the two can disagree), and
    (b) never validated that the client-supplied `doctor_id`/`room_id`
    actually belong to that branch, letting a cross-branch resource get
    silently attached to an appointment tagged with the caller's own branch.
    Both are fixed here to match the convention Pharmacy/Ward/Billing/
    Notifications already established."""
    branch_id_str = get_caller_branch_id(current_user)
    if branch_id_str is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no branch assigned")
    branch_id = uuid.UUID(branch_id_str)

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

    request = BookingRequest(
        branch_id=branch_id,
        patient_id=payload.patient_id,
        doctor_id=payload.doctor_id,
        room_id=payload.room_id,
        start_time=payload.start_time,
        duration_minutes=payload.duration_minutes,
        equipment_ids=payload.equipment_ids,
    )
    try:
        appointment = book_appointment(db, request)
    except BookingConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    return AppointmentResponse.model_validate(appointment)


@router.get("")
def list_appointments(
    branch_id: uuid.UUID,
    department_id: int | None = None,
    date: date | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> list[AppointmentListItemResponse]:
    """GET /api/v1/appointments?branch_id=&department_id=&date= (frontend
    UUID-to-dropdown conversion follow-up, backend phase). `date` defaults to
    today (UTC) when omitted. The `date` parameter name shadows the
    module-level `date` type within this function's body only -- same
    precedent routers/wards.py's `list_beds` sets with its `status` param
    shadowing the `fastapi.status` import in that file."""
    resolved_date = date if date is not None else datetime.now(timezone.utc).date()
    return appointment_service.list_appointments(db, current_user, branch_id, department_id, resolved_date)


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_appointment(
    appointment_id: str,
    reason: str = "cancelled_by_staff",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("front_desk", "system_admin", "doctor")),
) -> None:
    """Whole-system review finding C1 (Critical): this endpoint used to have
    NO ownership/branch check at all beyond role membership -- any
    front_desk/doctor/system_admin could cancel any appointment at any
    branch. `authorize()` (added below) runs the tenant guard against
    `appointment.branch_id`, matching the "authorize() before mutate"
    convention every other branch-scoped module already follows."""
    from app.models.appointment import Appointment  # local import avoids router-level circularity

    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "appointment not found")
    authorize(current_user, "appointment", "write", appointment)
    cancel_appointment(db, appointment, reason)
