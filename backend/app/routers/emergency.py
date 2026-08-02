from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.appointment import AppointmentResponse, EmergencyBookingCreate
from app.services.emergency_engine import EmergencyRequest, NoCapacityError, preempt_and_book
from app.services.scheduling_engine import BookingConflictError, ResourceBusyError

router = APIRouter(prefix="/api/v1/emergency", tags=["emergency"])


@router.post("/book", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def book_emergency(
    payload: EmergencyBookingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("nurse", "front_desk", "doctor", "system_admin")),
) -> AppointmentResponse:
    if current_user.branch_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no branch assigned")

    request = EmergencyRequest(
        branch_id=current_user.branch_id,
        patient_id=payload.patient_id,
        specialty_id=payload.specialty_id,
        triage_level=payload.triage_level,
        room_type=payload.room_type,
        equipment_ids=payload.equipment_ids,
        duration_minutes=payload.duration_minutes,
    )
    try:
        appointment = preempt_and_book(db, request)
    except NoCapacityError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except BookingConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    return AppointmentResponse.model_validate(appointment)
