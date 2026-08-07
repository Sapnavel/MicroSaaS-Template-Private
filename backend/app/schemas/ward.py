"""Pydantic request/response schemas for the Ward, Bed & OT module
(PRPs/ward-bed-ot-module-prp.md, Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT (see the
Phase 2 brief's "THE API CONTRACT" section) -- do not rename fields without
updating the frontend in lockstep.

`AdmissionResponse`/`OTScheduleResponse` deliberately do NOT declare
`stay_range`/`time_range` fields -- those are `TSTZRANGE` columns on the ORM
side (psycopg2 `Range` objects once loaded), and the wire contract wants
plain `start_time`/`end_time` datetimes instead. `services/ward_service.py`
extracts `.lower`/`.upper` off the loaded range (see
`ward_engine._lower_bound_isoformat` for the same idea applied to rebuilding
a range literal) and constructs these response models field-by-field rather
than relying on `model_validate(..., from_attributes=True)` against the ORM
row directly, since there is no `start_time`/`end_time` attribute on
`Admission`/`OTSchedule` for that to pick up.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Requests ----------------------------------------------------------------


class AdmissionCreate(BaseModel):
    """POST /api/v1/wards/admissions body."""

    model_config = ConfigDict(extra="forbid")

    patient_id: uuid.UUID
    bed_id: uuid.UUID
    start_time: datetime


class DischargeRequest(BaseModel):
    """PATCH /api/v1/wards/admissions/{id}/discharge body. `discharge_time`
    omitted/`null` means "now" -- `ward_engine.discharge_patient` defaults it
    server-side."""

    model_config = ConfigDict(extra="forbid")

    discharge_time: datetime | None = None


class TransferRequest(BaseModel):
    """PATCH /api/v1/wards/admissions/{id}/transfer body."""

    model_config = ConfigDict(extra="forbid")

    new_bed_id: uuid.UUID


class BedStatusUpdateRequest(BaseModel):
    """PATCH /api/v1/wards/beds/{id}/status body. Only the two manually
    reachable target statuses are accepted at the wire level -- `occupied`
    and `cleaning` are never legal targets of this endpoint (see
    `ward_engine._LEGAL_BED_TRANSITIONS`), so restricting the `Literal` here
    gives a clean 422 for those instead of a round-trip to the engine just to
    get a 409."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "blocked"]


class OTScheduleCreate(BaseModel):
    """POST /api/v1/wards/ot-schedule body."""

    model_config = ConfigDict(extra="forbid")

    room_id: uuid.UUID
    patient_id: uuid.UUID
    surgeon_id: uuid.UUID
    start_time: datetime
    duration_minutes: int = Field(gt=0, le=1440)


# --- Responses -----------------------------------------------------------


class AdmissionResponse(BaseModel):
    """Shared shape for admit/discharge/transfer responses -- `transfer`
    returns the NEW admission in this same shape (see
    `services/ward_engine.transfer_patient`'s docstring)."""

    id: uuid.UUID
    patient_id: uuid.UUID
    bed_id: uuid.UUID
    admitted_by: uuid.UUID
    start_time: datetime
    end_time: datetime | None
    discharged_at: datetime | None


class BedResponse(BaseModel):
    """PATCH /api/v1/wards/beds/{id}/status response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ward_id: uuid.UUID
    label: str
    status: str


class BedMatrixEntryResponse(BaseModel):
    """GET /api/v1/wards/beds response line. Built by hand in
    `services/ward_service.list_beds` from a `Bed` joined to its `Ward` (not
    plain `from_attributes` off a `Bed` row alone -- `ward_name`/`branch_id`
    live on `Ward`, not `Bed`).

    `active_admission_id` is additive (frontend UUID-to-dropdown conversion
    follow-up, backend phase): the id of the currently-open `Admission` row
    for this bed (`discharged_at IS NULL`), or `None` if the bed has no open
    admission. Lets the frontend read the admission id directly off the bed
    row for discharge/transfer instead of asking the user to type it in by
    hand. At most one open admission can exist per bed at any time --
    `Admission.__table_args__`'s `EXCLUDE USING gist (bed_id WITH =,
    stay_range WITH &&)` constraint (models/ward.py) guarantees this, so the
    left join `list_beds` uses to populate this field can never fan out into
    more than one row per bed."""

    id: uuid.UUID
    ward_id: uuid.UUID
    ward_name: str
    branch_id: uuid.UUID
    label: str
    status: str
    active_admission_id: uuid.UUID | None = None


class WardResponse(BaseModel):
    """GET /api/v1/wards response line (frontend UUID-to-dropdown conversion
    follow-up, backend phase)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    ward_type: str
    branch_id: uuid.UUID


class OTScheduleResponse(BaseModel):
    """POST/GET /api/v1/wards/ot-schedule response shape."""

    id: uuid.UUID
    room_id: uuid.UUID
    patient_id: uuid.UUID
    surgeon_id: uuid.UUID
    start_time: datetime
    end_time: datetime
