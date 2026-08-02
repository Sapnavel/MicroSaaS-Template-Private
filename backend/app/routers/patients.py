"""Patient Master Index module: create/search/read, dedup review queue,
merge/reject. Business logic lives in services/patient_service.py -- this
module is request validation + response shaping only, matching the pattern
in routers/auth.py / routers/appointments.py.

ABAC note: `patients` has no `branch_id` (see
services/patient_matching.py's module docstring and
services/patient_service.py's module docstring) -- `authorize()`'s tenant
guard is a no-op here by design. `require_role(...)` on each endpoint below
gates *which roles can reach this module at all*; role-based response
shaping (`patient_service.shape_patient_response`) is what actually enforces
"front_desk reads demographics only" per patient row.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.rate_limit import check_patient_search_rate_limit
from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.patient import (
    DuplicateCandidateResponse,
    MergeRequest,
    MergeResponse,
    PatientCreate,
    PatientDemographics,
    PatientFullRecord,
)
from app.services import patient_service

router = APIRouter(prefix="/api/v1/patients", tags=["patients"])

_READ_ROLES = ("front_desk", "nurse", "doctor", "system_admin")
_REVIEW_ROLES = ("front_desk", "system_admin")


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
def create_patient(
    payload: PatientCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> PatientDemographics | PatientFullRecord | JSONResponse:
    """Create a patient. Runs the deterministic dedup check first (409
    unless `force=true`, see `DeterministicMatchConflict`), then the
    probabilistic scan post-commit.

    The 409 body is built by hand here, NOT via `HTTPException(detail=...)`
    -- FastAPI always wraps `HTTPException.detail` as
    `{"detail": <value>}`, but the API contract requires
    `existing_patient_id`/`existing_patient_mrn` as top-level response keys
    alongside `detail`, so a plain HTTPException can't produce this shape.
    """
    try:
        patient = patient_service.create_patient(db, current_user, payload)
    except patient_service.DeterministicMatchConflict as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "a patient matching this phone number, national ID, or "
                "date of birth + name already exists",
                "existing_patient_id": str(exc.existing_patient.id),
                "existing_patient_mrn": exc.existing_patient.mrn,
            },
        )
    return patient_service.shape_patient_response(current_user, patient)


@router.get("/search")
def search_patients(
    name: str | None = None,
    dob: date | None = None,
    phone: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> list[PatientDemographics | PatientFullRecord]:
    """Search-before-create. At least one of `name`/`dob`/`phone` is
    required -- an unbounded scan of all patients is not an acceptable
    search, so no params given is a 422, not "return everything".

    Rate-limited per user (REVIEW-AGENT finding M1): this endpoint lets
    staff confirm whether a phone number belongs to an existing patient,
    which is PHI-adjacent enumeration if left unthrottled.
    """
    check_patient_search_rate_limit(str(current_user.id))
    if not name and not dob and not phone:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "at least one of name, dob, phone is required",
        )
    patients = patient_service.search_patients(db, name=name, dob=dob, phone=phone)
    return [patient_service.shape_patient_response(current_user, patient) for patient in patients]


@router.get("/duplicates")
def list_duplicates(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_REVIEW_ROLES)),
) -> list[DuplicateCandidateResponse]:
    """Pending (`status == 'pending'`) candidate review queue. Always
    demographics-shape for patient_a/patient_b regardless of caller role --
    see `DuplicateCandidateResponse` docstring."""
    return patient_service.list_pending_duplicates(db)


@router.get("/{patient_id}")
def get_patient(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> PatientDemographics | PatientFullRecord:
    patient = patient_service.get_patient(db, patient_id)
    return patient_service.shape_patient_response(current_user, patient)


@router.post("/duplicates/{candidate_id}/merge")
def merge_duplicate(
    candidate_id: uuid.UUID,
    payload: MergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_REVIEW_ROLES)),
) -> MergeResponse:
    survivor, reassigned_appointments = patient_service.merge_patients(
        db, candidate_id, payload.survivor_id, current_user.id
    )
    return MergeResponse(
        survivor=PatientFullRecord.model_validate(survivor),
        reassigned_appointments=reassigned_appointments,
    )


@router.post("/duplicates/{candidate_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_duplicate(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_REVIEW_ROLES)),
) -> None:
    patient_service.reject_duplicate_candidate(db, candidate_id, current_user.id)
