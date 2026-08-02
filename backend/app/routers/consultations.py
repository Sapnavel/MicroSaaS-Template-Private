"""Clinical Consultation & Smart Prescription Engine module: consultations,
diagnoses, prescriptions (safety-gated), and patient allergies. Business
logic lives in services/consultation_service.py -- this module is request
validation + response shaping only, matching the pattern in
routers/patients.py.

TWO routers in this one file (a judgment call, documented here per the
Phase 2 brief): `router` (prefix `/api/v1/consultations`) owns the
consultation/diagnosis/prescription endpoints, and `patient_allergy_router`
(prefix `/api/v1/patients`) owns `GET`/`POST .../{patient_id}/allergies`.
They don't collide with `routers/patients.py`'s existing
`/api/v1/patients/{patient_id}` catch-all -- FastAPI matches by exact path
template and segment count, so `/api/v1/patients/{patient_id}/allergies` is
unambiguous -- but the allergy endpoints are kept as their own router here,
not merged into `routers/patients.py`, because they're conceptually owned
by this module (they exist to feed `services/prescription_safety.py`'s
allergy check, and nothing in the Patient module reads/writes
`patient_allergies`). A second *file* was considered and rejected: these
endpoints are small, share this module's service layer and schemas, and
splitting them out would add navigation overhead with no real
separation-of-concerns benefit over a second `APIRouter()` instance here.
Both routers are registered in `main.py`.

ABAC note: `consultations` has no `branch_id` (see models/consultation.py's
module docstring) -- `authorize()`'s tenant guard is a no-op here, same as
Patient. `require_role(...)` on each endpoint gates *which roles can reach
this module at all*; the registered `consultation` read/write ABAC policies
(core/security.py) -- called from within consultation_service.py, not here
-- are what actually enforce "doctor reads/writes only their own
consultations".
"""

import uuid

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.consultation import (
    AllergyCreate,
    AllergyResponse,
    ConsultationCompleteRequest,
    ConsultationCreate,
    ConsultationResponse,
    DiagnosisCreate,
    DiagnosisResponse,
    PrescriptionCreateRequest,
    PrescriptionCreateResponse,
    PrescriptionItemResponse,
    PrescriptionRecord,
    PrescriptionResponse,
    PrescriptionSafetyReportResponse,
    SafetyFindingResponse,
)
from app.services import consultation_service
from app.services.prescription_safety import SafetyFinding, SafetyTier

router = APIRouter(prefix="/api/v1/consultations", tags=["consultations"])
patient_allergy_router = APIRouter(prefix="/api/v1/patients", tags=["patient-allergies"])

_DOCTOR_ADMIN = ("doctor", "system_admin")
_DOCTOR_NURSE_ADMIN = ("doctor", "nurse", "system_admin")


def _finding_response_dict(finding: SafetyFinding) -> dict[str, object]:
    """Plain-JSON-serializable shape of a `SafetyFinding` for a hand-built
    `JSONResponse` body (409/422 branches below) -- `model_dump(mode="json")`
    converts the `uuid.UUID`s to JSON-safe primitives, which raw
    `JSONResponse(content=...)` does not do on its own.

    BUG FOUND BY TEST-AGENT (Phase 3, PRPs/clinical-consultation-prescription-
    prp.md): `SafetyFindingResponse.model_validate(finding)` (the original
    form of this function) 500s. `SafetyFinding.tier` is a `SafetyTier`
    (`str`-mixin `Enum`) member, and `SafetyFindingResponse.tier` is a plain
    `Literal["BLOCK", "OVERRIDE_REQUIRED", "INFO"]` -- pydantic-core's
    literal validator rejects the enum member even though
    `SafetyTier.BLOCK == "BLOCK"` is `True` in plain Python (confirmed
    directly: `model_validate` on an object whose `.tier` attribute is a
    `str`-Enum member raises `literal_error`, not a silent pass-through).
    This broke the 422 (BLOCK) and 409 (OVERRIDE_REQUIRED) response bodies
    unconditionally -- i.e. the two core safety-gate rejection paths this
    whole module exists for. Fixed by constructing the response model with
    `tier=finding.tier.value` (a plain `str`) explicitly instead of relying
    on `from_attributes` to coerce it."""
    return SafetyFindingResponse(
        source=finding.source,
        severity=finding.severity,
        tier=finding.tier.value,
        message=finding.message,
        drug_ids=list(finding.drug_ids),
    ).model_dump(mode="json")


@router.post("", status_code=status.HTTP_201_CREATED)
def create_consultation(
    payload: ConsultationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_ADMIN)),
) -> ConsultationResponse:
    """Start a consultation from an `in_progress` appointment.
    `doctor_id`/`patient_id` come from the appointment, not from
    `current_user` or the request body."""
    consultation = consultation_service.start_consultation(db, current_user, payload)
    return consultation_service.build_consultation_response(db, consultation)


@router.get("/{consultation_id}")
def get_consultation(
    consultation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_NURSE_ADMIN)),
) -> ConsultationResponse:
    consultation = consultation_service.get_consultation(db, current_user, consultation_id)
    return consultation_service.build_consultation_response(db, consultation)


@router.patch("/{consultation_id}/complete")
def complete_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationCompleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_ADMIN)),
) -> ConsultationResponse:
    consultation = consultation_service.complete_consultation(db, current_user, consultation_id, payload)
    return consultation_service.build_consultation_response(db, consultation)


@router.post("/{consultation_id}/diagnoses", status_code=status.HTTP_201_CREATED)
def add_diagnosis(
    consultation_id: uuid.UUID,
    payload: DiagnosisCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_ADMIN)),
) -> DiagnosisResponse:
    diagnosis = consultation_service.add_diagnosis(db, current_user, consultation_id, payload)
    return DiagnosisResponse.model_validate(diagnosis)


@router.post("/{consultation_id}/prescriptions", status_code=status.HTTP_201_CREATED, response_model=None)
def create_prescription(
    consultation_id: uuid.UUID,
    payload: PrescriptionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_ADMIN)),
) -> PrescriptionCreateResponse | JSONResponse:
    """The safety-gated prescription flow (PRPs/clinical-consultation-
    prescription-prp.md, "SAFETY ENGINE DESIGN"). The 422/409 bodies below
    are built by hand, NOT via `HTTPException(detail=...)` -- FastAPI always
    wraps `.detail` as `{"detail": ...}`, but the API contract requires
    `blocking_findings`/`override_required_findings` as top-level response
    keys alongside `detail`, matching the same body-shape discipline
    `routers/patients.py`'s `create_patient` uses for its 409."""
    try:
        prescription, items, report = consultation_service.create_prescription(
            db, current_user, consultation_id, payload
        )
    except consultation_service.PrescriptionBlocked as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "one or more requested drugs are blocked by a contraindicated "
                "interaction or a severe allergy; this prescription cannot be finalized",
                "blocking_findings": [_finding_response_dict(f) for f in exc.findings],
            },
        )
    except consultation_service.PrescriptionOverrideRequired as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "one or more findings require an explicit override (override=true "
                "and a non-empty override_reason) before this prescription can be finalized",
                "override_required_findings": [_finding_response_dict(f) for f in exc.findings],
            },
        )

    # Only INFO-tier findings are surfaced on a 201 -- BLOCK/OVERRIDE_REQUIRED
    # findings mean this line was never reached without an override (and the
    # findings that triggered that override were already shown to the caller
    # in the preceding 409, plus recorded in full in the audit log).
    info_allergy = [f for f in report.allergy_findings if f.tier == SafetyTier.INFO]
    info_interaction = [f for f in report.interaction_findings if f.tier == SafetyTier.INFO]

    return PrescriptionCreateResponse(
        prescription=PrescriptionRecord.model_validate(prescription),
        items=[PrescriptionItemResponse.model_validate(i) for i in items],
        safety_report=PrescriptionSafetyReportResponse(
            allergy_findings=[SafetyFindingResponse.model_validate(f) for f in info_allergy],
            interaction_findings=[SafetyFindingResponse.model_validate(f) for f in info_interaction],
        ),
    )


@router.get("/{consultation_id}/prescriptions")
def list_prescriptions(
    consultation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_NURSE_ADMIN)),
) -> list[PrescriptionResponse]:
    prescriptions_with_items = consultation_service.list_prescriptions(db, current_user, consultation_id)
    return [
        PrescriptionResponse(
            id=p.id,
            status=p.status,
            created_at=p.created_at,
            items=[PrescriptionItemResponse.model_validate(i) for i in items],
        )
        for p, items in prescriptions_with_items
    ]


# --- Patient allergies -------------------------------------------------------


@patient_allergy_router.get("/{patient_id}/allergies")
def list_allergies(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_NURSE_ADMIN)),
) -> list[AllergyResponse]:
    allergies = consultation_service.list_allergies(db, patient_id)
    return [AllergyResponse.model_validate(a) for a in allergies]


@patient_allergy_router.post("/{patient_id}/allergies", status_code=status.HTTP_201_CREATED)
def add_allergy(
    patient_id: uuid.UUID,
    payload: AllergyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_DOCTOR_NURSE_ADMIN)),
) -> AllergyResponse:
    """No ownership restriction beyond role -- any doctor/nurse/system_admin
    may record an allergy for any patient; REVIEW-AGENT assessed this as
    correct (see consultation_service.add_allergy's docstring), while
    adding an audit-log entry for the write, which was the actual gap."""
    allergy = consultation_service.add_allergy(db, current_user, patient_id, payload)
    return AllergyResponse.model_validate(allergy)
