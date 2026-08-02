"""Pydantic response schema for the Notification & Alert Hub module
(PRPs/notification-hub-prp.md, Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT, working
in parallel against the same PRP -- do not rename fields without updating
the frontend in lockstep.

Only a response schema exists here: there is no request body schema because
neither endpoint takes one (`GET /notifications` takes query params only,
`PATCH /notifications/{id}/retry` takes no body) -- see
`services/notification_service.py` / `routers/notifications.py`.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    """Shared shape returned by both endpoints (list + retry)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    patient_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    channel: str
    template: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
