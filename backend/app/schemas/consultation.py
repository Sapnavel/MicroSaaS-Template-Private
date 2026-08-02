"""Pydantic request/response schemas for the Clinical Consultation & Smart
Prescription Engine module (PRPs/clinical-consultation-prescription-prp.md,
Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT (see the
"THE API CONTRACT" section of the Phase 2 brief) -- do not rename fields
without updating the frontend in lockstep.

Response-shape judgment call (flagged per the Phase 2 brief -- "consider
whether you want one schema or two, e.g. a summary vs. detail response"):
a SINGLE `ConsultationResponse` is used for the create (POST), read (GET),
and complete (PATCH) endpoints, not a summary/detail split. The API
contract gives all three the *same* field set -- `diagnoses`/`prescriptions`
are simply empty lists right after creation and populated once diagnoses/
prescriptions are added -- so a summary/detail split would just be the same
schema with different data, not different fields. `services/
consultation_service.py`'s `build_consultation_response` is the single place
that assembles it (same "one function shapes the response" discipline as
`patient_service.shape_patient_response`).

Nested vs. standalone shape judgment call: `DiagnosisSummary`/
`PrescriptionResponse` (nested inside `ConsultationResponse`, and used for
the standalone list endpoints) intentionally omit `consultation_id` /
`patient_id` -- redundant once already scoped under a consultation URL.
`DiagnosisResponse` (POST .../diagnoses response) and `PrescriptionRecord`
(the "prescription" sub-object of the POST .../prescriptions response) are
separate schemas that add those ids back, matching the API contract's
top-level create-response shapes exactly (field-for-field, since the
frontend is built against this exact contract).

ICD code validation: a LOOSE format check only (`DiagnosisCreate.icd_code`,
validated in `services/consultation_service.py`'s `_ICD_CODE_PATTERN`) --
one letter, two digits, optional `.subcode` of 1-4 alphanumeric characters.
This is NOT a lookup against a real ICD-10/11 reference table (no such table
exists in this schema, and building one is out of scope per the PRP) --
it only rejects obviously-malformed input.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --- Consultations -----------------------------------------------------------


class ConsultationCreate(BaseModel):
    """POST /api/v1/consultations body. `doctor_id`/`patient_id` are NOT
    accepted here -- they're derived server-side from the referenced
    `appointment_id` (see `consultation_service.start_consultation`), never
    from client input or the caller's own identity."""

    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID
    symptoms: str | None = None


class ConsultationCompleteRequest(BaseModel):
    """PATCH /api/v1/consultations/{id}/complete body."""

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None


class DiagnosisSummary(BaseModel):
    """Nested shape used inside `ConsultationResponse.diagnoses` -- no
    `consultation_id`, see module docstring."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    icd_code: str
    description: str
    is_primary: bool


class PrescriptionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    drug_id: UUID
    dosage: str
    frequency: str
    duration_days: int | None


class PrescriptionResponse(BaseModel):
    """Nested shape used inside `ConsultationResponse.prescriptions`, AND
    the item shape for GET /consultations/{id}/prescriptions -- both are the
    same shape per the API contract (no `consultation_id`/`patient_id`,
    already scoped by the URL). See module docstring."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    created_at: datetime
    items: list[PrescriptionItemResponse]


class ConsultationResponse(BaseModel):
    """Shared shape for POST /consultations, GET /consultations/{id}, and
    PATCH /consultations/{id}/complete -- see module docstring for why this
    is one schema, not a summary/detail split."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    doctor_id: UUID
    patient_id: UUID
    symptoms: str | None
    notes: str | None
    started_at: datetime
    ended_at: datetime | None
    diagnoses: list[DiagnosisSummary]
    prescriptions: list[PrescriptionResponse]


# --- Diagnoses ---------------------------------------------------------------


class DiagnosisCreate(BaseModel):
    """POST /api/v1/consultations/{id}/diagnoses body. `icd_code` gets a
    loose format check only -- see module docstring."""

    model_config = ConfigDict(extra="forbid")

    icd_code: str = Field(min_length=1)
    description: str = Field(min_length=1)
    is_primary: bool = False


class DiagnosisResponse(DiagnosisSummary):
    """POST /consultations/{id}/diagnoses response -- adds `consultation_id`
    back on top of `DiagnosisSummary`, per the API contract's standalone
    create-response shape."""

    consultation_id: UUID


# --- Allergies -----------------------------------------------------------


class AllergyCreate(BaseModel):
    """POST /api/v1/patients/{patient_id}/allergies body. `severity` is
    restricted to the three values the safety engine's classification table
    (`prescription_safety._ALLERGY_SEVERITY_TIER`) actually understands --
    anything else 422s here rather than silently falling through to that
    module's fail-safe-to-OVERRIDE_REQUIRED default for unrecognized data."""

    model_config = ConfigDict(extra="forbid")

    substance: str = Field(min_length=1)
    severity: Literal["mild", "moderate", "severe"]
    reaction: str | None = None


class AllergyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    substance: str
    severity: str
    reaction: str | None


# --- Prescriptions -------------------------------------------------------


class PrescriptionItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drug_id: UUID
    dosage: str = Field(min_length=1)
    frequency: str = Field(min_length=1)
    # ge=1: a zero/negative duration_days would push
    # prescription_safety._active_prescription_drug_ids' course_end into the
    # past immediately, silently making the interaction checker treat this
    # drug as never-active -- an ordinary typo could defeat the safety
    # engine's input, not by touching the engine itself (REVIEW-AGENT
    # finding H1, PRPs/clinical-consultation-prescription-prp.md Phase 3).
    duration_days: int | None = Field(default=None, ge=1)


class PrescriptionCreateRequest(BaseModel):
    """POST /api/v1/consultations/{id}/prescriptions body. `items` requires
    at least one entry -- an empty prescription has no meaning (a judgment
    call: not explicitly specified by the API contract, but nothing
    downstream -- the safety engine, the persisted rows -- makes sense for
    a zero-item request)."""

    model_config = ConfigDict(extra="forbid")

    items: list[PrescriptionItemCreate] = Field(min_length=1)
    override: bool = False
    override_reason: str | None = None


class PrescriptionRecord(BaseModel):
    """The "prescription" sub-object of the POST .../prescriptions 201
    response -- adds `consultation_id`/`patient_id` back on top of the
    nested `PrescriptionResponse` shape, per the API contract's standalone
    create-response shape (no `items` here -- those are a separate
    top-level `items` key in that response, see `PrescriptionCreateResponse`)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    consultation_id: UUID
    patient_id: UUID
    status: str
    created_at: datetime


class SafetyFindingResponse(BaseModel):
    """Wire shape for one `prescription_safety.SafetyFinding`. `tier` is
    restricted to the three real values `SafetyTier` can hold; `severity` is
    left as a plain `str` since it's the raw DB value (e.g. "major",
    "severe"), not the derived classification."""

    model_config = ConfigDict(from_attributes=True)

    source: Literal["allergy", "interaction"]
    severity: str
    tier: Literal["BLOCK", "OVERRIDE_REQUIRED", "INFO"]
    message: str
    drug_ids: list[UUID]


class PrescriptionSafetyReportResponse(BaseModel):
    """The "safety_report" key of the POST .../prescriptions 201 response.
    Only ever populated with INFO-tier findings on a 201 -- see
    `routers/consultations.py`'s `create_prescription` and the API
    contract's note that BLOCK/OVERRIDE_REQUIRED findings mean the request
    doesn't reach 201 without an override in the first place."""

    allergy_findings: list[SafetyFindingResponse]
    interaction_findings: list[SafetyFindingResponse]


class PrescriptionCreateResponse(BaseModel):
    """POST /api/v1/consultations/{id}/prescriptions 201 response."""

    prescription: PrescriptionRecord
    items: list[PrescriptionItemResponse]
    safety_report: PrescriptionSafetyReportResponse
