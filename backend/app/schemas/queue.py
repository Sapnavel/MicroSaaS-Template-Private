"""Pydantic request/response schemas for the Real-Time Queue & Digital Token
module (PRPs/realtime-queue-prp.md, Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT, working
in parallel against the same PRP -- do not rename fields without updating
the frontend in lockstep.

`QueueCheckInRequest` enforces "exactly one of `appointment_id` OR
(`branch_id` AND `department_id`)" itself, via a `model_validator` -- this is
wire-level validation only; `services/queue_service.py`'s internal
`QueueCheckInPayload` dataclass (Phase 1) does not re-validate this rule, it
only consumes whichever shape already passed here (see that module's
docstring).

`StatusUpdateRequest.status` is a `Literal` of `TokenStatus`'s value set, the
same "restrict the wire-level string so an invalid value gets a clean 422
instead of a round-trip to the service just to get a 409" discipline
`schemas/ward.py`'s `BedStatusUpdateRequest` uses. The string -> `TokenStatus`
enum conversion itself happens at the router boundary (`TokenStatus(payload.status)`),
mirroring `services/billing_service.set_claim_state`'s
`ClaimState(payload.state)` conversion for the same shape of problem --
there is no separate service-layer function between the router and
`queue_service` here for that conversion to live in instead.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

# --- Requests ----------------------------------------------------------------


class QueueCheckInRequest(BaseModel):
    """POST /api/v1/queue/check-in body. Exactly one of `appointment_id` OR
    (`branch_id` AND `department_id`) must be supplied -- see
    `services/queue_service.py`'s module docstring for the two check-in
    paths this maps to (appointment-linked vs. walk-in)."""

    model_config = ConfigDict(extra="forbid")

    appointment_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    department_id: int | None = None

    @model_validator(mode="after")
    def _validate_exactly_one_shape(self) -> "QueueCheckInRequest":
        has_appointment = self.appointment_id is not None
        has_walk_in = self.branch_id is not None and self.department_id is not None
        has_partial_walk_in = (self.branch_id is not None) != (self.department_id is not None)

        if has_partial_walk_in:
            raise ValueError("branch_id and department_id must both be supplied together for a walk-in check-in")
        if has_appointment and has_walk_in:
            raise ValueError(
                "supply exactly one of appointment_id OR (branch_id and department_id), not both"
            )
        if not has_appointment and not has_walk_in:
            raise ValueError("supply exactly one of appointment_id OR (branch_id and department_id)")
        return self


class StatusUpdateRequest(BaseModel):
    """PATCH /api/v1/queue/{token_id}/status body. `services/queue_service.py`'s
    `_LEGAL_TRANSITIONS` is the actual authority on which `(current,
    requested)` pairs are legal -- a 409 `IllegalTokenStatusTransition`
    otherwise. The `Literal` here only rules out a status string that isn't
    even one of `TokenStatus`'s values."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["waiting", "in_consultation", "delayed", "skipped", "done"]


# --- Responses -----------------------------------------------------------


class QueueTokenResponse(BaseModel):
    """Shared shape returned by check-in, status update, and the board
    snapshot -- mirrors `models/queue.py`'s `QueueToken` fields exactly."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    branch_id: uuid.UUID
    appointment_id: uuid.UUID | None
    department_id: int | None
    token_number: int
    status: str
    checked_in_at: datetime
    called_at: datetime | None
    completed_at: datetime | None
    estimated_wait_minutes: int | None
