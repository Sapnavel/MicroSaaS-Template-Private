"""Ward, Bed & OT module (PRPs/ward-bed-ot-module-prp.md, Phase 2). Business
logic lives in services/ward_service.py -- this module is request
validation, exception-to-status-code mapping, and response shaping only,
matching the pattern in routers/appointments.py / routers/patients.py.

Exception -> status code mapping (mirrors routers/appointments.py's
`BookingConflictError` -> 409 / `ResourceBusyError` -> 423 convention,
applied to this module's analogous `ward_engine` exceptions):
    BedNotFoundError / AdmissionNotFoundError   -> 404
    BedNotAvailableError / BedConflictError /
        AlreadyDischargedError                  -> 409 (plain {"detail": ...})
    IllegalBedStatusTransition                  -> 409, hand-built JSONResponse
                                                    ({"detail", "current", "requested"})
    OTConflictError                             -> 409, hand-built JSONResponse
                                                    ({"detail", "reason"})
    ResourceBusyError                           -> 423 (lock contention, same
                                                    convention as appointments.py)

Two endpoints (`update_bed_status`, `create_ot_schedule`) build their 409
body by hand via `JSONResponse`, NOT `HTTPException(detail=...)` -- FastAPI
always wraps `HTTPException.detail` as `{"detail": <value>}`, but the API
contract needs `current`/`requested` (bed status) or `reason` (OT) as extra
top-level keys, the same body-shape discipline routers/patients.py's
`create_patient` and routers/consultations.py's `create_prescription` use
for their own structured 409/422 bodies.

`services/ward_service.py` resolves branch scoping/`authorize()` calls
itself (walking Bed -> Ward -> branch_id, or using Room.branch_id directly
for OT) -- this router does not duplicate that logic, it only gates *which
roles can reach each endpoint at all* via `require_role`.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.models.ward import BedStatus
from app.schemas.ward import (
    AdmissionCreate,
    AdmissionResponse,
    BedMatrixEntryResponse,
    BedResponse,
    BedStatusUpdateRequest,
    DischargeRequest,
    OTScheduleCreate,
    OTScheduleResponse,
    TransferRequest,
    WardResponse,
)
from app.services import ward_service
from app.services.ward_engine import (
    AdmissionNotFoundError,
    AlreadyDischargedError,
    BedConflictError,
    BedNotAvailableError,
    BedNotFoundError,
    IllegalBedStatusTransition,
    OTConflictError,
    ResourceBusyError,
)

router = APIRouter(prefix="/api/v1/wards", tags=["wards"])

_BED_READ_ROLES = ("nurse", "doctor", "front_desk", "system_admin")
_ADMISSION_WRITE_ROLES = ("nurse", "doctor", "system_admin")
_BED_STATUS_WRITE_ROLES = ("nurse", "system_admin")
_OT_WRITE_ROLES = ("doctor", "system_admin")
_OT_READ_ROLES = ("doctor", "nurse", "system_admin")


@router.get("/beds")
def list_beds(
    branch_id: uuid.UUID | None = None,
    ward_id: uuid.UUID | None = None,
    status: Literal["available", "occupied", "cleaning", "blocked"] | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_BED_READ_ROLES)),
) -> list[BedMatrixEntryResponse]:
    return ward_service.list_beds(
        db,
        current_user,
        branch_id,
        ward_id,
        BedStatus(status) if status is not None else None,
    )


@router.get("")
def list_wards(
    branch_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_BED_READ_ROLES)),
) -> list[WardResponse]:
    """GET /api/v1/wards?branch_id= (required; frontend UUID-to-dropdown
    conversion follow-up, backend phase). Same read-roles as `GET /beds`
    above."""
    return ward_service.list_wards(db, current_user, branch_id)


@router.post("/admissions", status_code=status.HTTP_201_CREATED)
def create_admission(
    payload: AdmissionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ADMISSION_WRITE_ROLES)),
) -> AdmissionResponse:
    try:
        return ward_service.admit(db, current_user, payload)
    except (BedNotAvailableError, BedConflictError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    except BedNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch("/admissions/{admission_id}/discharge")
def discharge_admission(
    admission_id: uuid.UUID,
    payload: DischargeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ADMISSION_WRITE_ROLES)),
) -> AdmissionResponse:
    try:
        return ward_service.discharge(db, current_user, admission_id, payload)
    except AdmissionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AlreadyDischargedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc


@router.patch("/admissions/{admission_id}/transfer")
def transfer_admission(
    admission_id: uuid.UUID,
    payload: TransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ADMISSION_WRITE_ROLES)),
) -> AdmissionResponse:
    try:
        return ward_service.transfer(db, current_user, admission_id, payload)
    except AdmissionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (AlreadyDischargedError, BedConflictError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc


@router.patch("/beds/{bed_id}/status", response_model=None)
def update_bed_status(
    bed_id: uuid.UUID,
    payload: BedStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_BED_STATUS_WRITE_ROLES)),
) -> BedResponse | JSONResponse:
    """The 409 body is built by hand here, NOT via `HTTPException(detail=...)`
    -- the API contract requires `current`/`requested` as extra top-level
    response keys alongside `detail` (same discipline
    routers/patients.py's `create_patient` uses)."""
    try:
        return ward_service.update_bed_status(db, current_user, bed_id, payload)
    except BedNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except IllegalBedStatusTransition as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "current": exc.current.value,
                "requested": exc.requested.value,
            },
        )
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc


@router.post("/ot-schedule", status_code=status.HTTP_201_CREATED, response_model=None)
def create_ot_schedule(
    payload: OTScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_OT_WRITE_ROLES)),
) -> OTScheduleResponse | JSONResponse:
    """The 409 body is built by hand here, NOT via `HTTPException(detail=...)`
    -- the API contract requires `reason` (one of `surgeon_busy_ot` /
    `surgeon_busy_appointment` / `room_conflict`) as an extra top-level
    response key alongside `detail` (same discipline
    routers/consultations.py's `create_prescription` uses)."""
    try:
        response = ward_service.schedule_ot(db, current_user, payload)
    except OTConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "reason": exc.reason},
        )
    except ResourceBusyError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    return response


@router.get("/ot-schedule")
def list_ot_schedule(
    room_id: uuid.UUID | None = None,
    surgeon_id: uuid.UUID | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_OT_READ_ROLES)),
) -> list[OTScheduleResponse]:
    return ward_service.list_ot_schedules(db, current_user, room_id, surgeon_id, from_time, to_time)
