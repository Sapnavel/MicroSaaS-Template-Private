"""Pydantic request/response schemas for the Patient Master Index module.

Field names/casing here are a contract shared with FRONTEND-AGENT (see the
"THE API CONTRACT" section of the Phase 2 brief for
PRPs/patient-master-index-prp.md) -- do not rename fields without updating
the frontend in lockstep.

Two response shapes exist for the *same* underlying `Patient` row --
`PatientDemographics` (front_desk) and `PatientFullRecord` (doctor/nurse/
system_admin, extends demographics with `national_id`/`address`/
`allergies_note`) -- because "front_desk reads demographics only" is a
response-shaping rule, not a row-access rule (patients have no `branch_id`
to gate on; see `services/patient_matching.py` module docstring / the PRP's
"Key design decision"). `services/patient_service.py`'s
`shape_patient_response` is the single place that picks between them so this
restriction can't be forgotten in one endpoint and not another.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PatientCreate(BaseModel):
    """POST /api/v1/patients body.

    `force=True` bypasses the 409-on-deterministic-match block (still
    creates a new row) -- staff judgment call for the rare legitimate case
    of two different people who happen to share a matched signal. Always
    audit-logged when it actually overrides a match
    (`patient_service.create_patient`, action="patient.force_create_despite_match").
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1)
    dob: date
    sex: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    national_id: str | None = None
    address: str | None = None
    allergies_note: str | None = None
    force: bool = False


class PatientDemographics(BaseModel):
    """front_desk's view of a patient: identity + contact only, no clinical
    fields (national_id, address, allergies_note are withheld)."""

    id: UUID
    mrn: str
    full_name: str
    dob: date
    sex: str
    phone: str
    user_id: UUID | None = None
    merged_into_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)


class PatientFullRecord(PatientDemographics):
    """doctor/nurse/system_admin's view: demographics plus clinical fields.

    All PHI fields here (`phone` inherited, `national_id`, `address`,
    `allergies_note`) are already plaintext by the time they reach this
    schema -- `EncryptedString` (app/core/encryption.py) decrypts
    transparently on ORM load, there is no separate "decrypt for API" step
    and none should be added here.
    """

    national_id: str | None = None
    address: str | None = None
    allergies_note: str | None = None


class DuplicateCandidateResponse(BaseModel):
    """GET /api/v1/patients/duplicates row shape.

    `patient_a`/`patient_b` are always demographics-shape regardless of the
    caller's role (front_desk and system_admin both hit this endpoint, and
    demographics-level detail -- name/dob/phone -- is all a reviewer needs
    to judge "is this the same person", per the endpoint contract) --
    deliberately NOT routed through `shape_patient_response`'s per-role
    branch, which is for single-patient reads, not this review queue.
    """

    id: UUID
    patient_a: PatientDemographics
    patient_b: PatientDemographics
    match_score: float
    match_reason: dict[str, Any]
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MergeRequest(BaseModel):
    survivor_id: UUID


class MergeResponse(BaseModel):
    """POST /duplicates/{id}/merge response. `survivor` is always
    full-record-shape regardless of caller role (front_desk included) --
    per the endpoint contract, distinct from the role-shaped responses
    everywhere else in this module. A front_desk reviewer who just
    confirmed a merge needs to see the resulting canonical record in full
    to verify the merge went as expected."""

    survivor: PatientFullRecord
    reassigned_appointments: int
