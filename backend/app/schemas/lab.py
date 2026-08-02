"""Pydantic request/response schemas for the Lab module (PRPs/lab-module-prp.md,
Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT (see the
Phase 2 brief's "THE API CONTRACT" section) -- do not rename fields without
updating the frontend in lockstep.

Role-shaped response judgment call (per the Phase 2 brief -- "make a call and
document it"): `nurse` callers get the `*NoResult` variants below (`result`
key absent from the JSON entirely, not `null`) -- nurses collect samples,
they don't need to see the lab value. `doctor`, `lab_tech`, `system_admin`
get the full shape including the decrypted `result`. This mirrors the
Patient module's front_desk/demographics-only split. The single place this
branch happens is `services/lab_service.shape_lab_order_response` (and its
batched sibling `shape_lab_order_responses`) -- every endpoint returning a
lab order calls through there so the restriction can't be forgotten on one
endpoint and not another.

`LabSampleResponse.result` is the decrypted plaintext -- `EncryptedString`
(models/lab.py) already handles decryption transparently on ORM load, so no
separate decrypt step happens here or in the service layer.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.lab import LabOrderStatus

# --- Requests ----------------------------------------------------------------


class LabOrderCreate(BaseModel):
    """POST /api/v1/lab/orders body. `patient_id`/`ordered_by` are NOT
    accepted here -- `patient_id` is derived server-side from the referenced
    consultation, `ordered_by` from `current_user.id` (see
    `services/lab_service.create_order`), never from client input."""

    model_config = ConfigDict(extra="forbid")

    consultation_id: UUID
    test_code: str = Field(min_length=1)


class TransitionRequest(BaseModel):
    """PATCH /api/v1/lab/orders/{id}/transition body. `result` is only
    meaningful/required for the transition arriving at `verified` --
    `services/lab_workflow.validate_result_for_transition` rejects it being
    present for any other transition (a client cannot sneak a result value
    in on an unrelated transition; see that module's docstring)."""

    model_config = ConfigDict(extra="forbid")

    to_status: Literal["collected", "processing", "verified", "attached"]
    result: str | None = None


# --- Responses -----------------------------------------------------------


class LabSampleResponse(BaseModel):
    """Full sample shape -- doctor/lab_tech/system_admin view, includes the
    decrypted `result`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collected_by: UUID | None
    collected_at: datetime | None
    processed_at: datetime | None
    verified_by: UUID | None
    verified_at: datetime | None
    result: str | None
    attached_to_emr_at: datetime | None


class LabSampleResponseNoResult(BaseModel):
    """Same shape as `LabSampleResponse` minus `result` -- the nurse view.
    The key is absent from the JSON entirely (not `null`), since this is a
    separate schema rather than `LabSampleResponse` with a field set to
    `None`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collected_by: UUID | None
    collected_at: datetime | None
    processed_at: datetime | None
    verified_by: UUID | None
    verified_at: datetime | None
    attached_to_emr_at: datetime | None


class LabOrderResponse(BaseModel):
    """Full order shape -- doctor/lab_tech/system_admin view. Used by the
    create/get-by-id/list/transition endpoints alike (see
    `services/lab_service.shape_lab_order_response`)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    consultation_id: UUID
    patient_id: UUID
    test_code: str
    ordered_by: UUID
    status: LabOrderStatus
    created_at: datetime
    sample: LabSampleResponse | None


class LabOrderResponseNoResult(BaseModel):
    """Nurse view -- same shape as `LabOrderResponse` but embeds
    `LabSampleResponseNoResult` (no `result` key) instead."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    consultation_id: UUID
    patient_id: UUID
    test_code: str
    ordered_by: UUID
    status: LabOrderStatus
    created_at: datetime
    sample: LabSampleResponseNoResult | None
