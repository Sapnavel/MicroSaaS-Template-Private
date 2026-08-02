from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_role
from app.database import get_db
from app.models.user import User
from app.schemas.appointment import AppointmentCreate, AppointmentResponse
from app.services.scheduling_engine import (
    BookingConflictError,
    BookingRequest,
    ResourceBusyError,
    book_appointment,
    cancel_appointment,
)

router = APIRouter(prefix="/api/v1/appointments", tags=["appointments"])


@router.post("", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def create_appointment(
    payload: AppointmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("front_desk", "system_admin", "doctor")),
) -> AppointmentResponse:
    if current_user.branch_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no branch assigned")

    request = BookingRequest(
        branch_id=current_user.branch_id,
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


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_appointment(
    appointment_id: str,
    reason: str = "cancelled_by_staff",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("front_desk", "system_admin", "doctor")),
) -> None:
    from app.models.appointment import Appointment  # local import avoids router-level circularity

    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "appointment not found")
    cancel_appointment(db, appointment, reason)
