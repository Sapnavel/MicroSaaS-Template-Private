"""Real-Time Queue & Digital Token module (PRPs/realtime-queue-prp.md, Phase
2). Business logic lives in services/queue_service.py -- this module is
request validation, role gating, exception-to-status-code mapping only,
matching the pattern in routers/wards.py / routers/notifications.py.

`check_in`/`update_status` are `async def` because `queue_service.check_in`/
`update_status` are themselves `async def` (they broadcast over the live
WebSocket channel -- see that module's docstring, design decision #4).
`get_board` stays plain sync `def`, mirroring `queue_service.list_queue`
(a plain read, never broadcasts).

Per the PRP's ENDPOINTS table: `front_desk`/`nurse`/`system_admin` may
check in and transition status; `doctor` is added to the read-only board
endpoint's role list alongside them (read-only queue visibility for a
doctor's own queue, cannot alter it). Fine-grained branch scoping is
enforced inside queue_service.py via authorize() (the tenant guard), not
here -- require_role below is only the coarse "which roles may call this
endpoint at all" gate, same split every other module's router uses.

Exception -> status code mapping:
    AppointmentNotFoundError / QueueTokenNotFoundError -> 404
    IllegalTokenStatusTransition                       -> 409
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.queue import TokenStatus
from app.models.user import User
from app.schemas.queue import QueueCheckInRequest, QueueTokenResponse, StatusUpdateRequest
from app.services import queue_service
from app.services.queue_service import (
    AppointmentNotFoundError,
    IllegalTokenStatusTransition,
    QueueCheckInPayload,
    QueueTokenNotFoundError,
)

router = APIRouter(prefix="/api/v1/queue", tags=["queue"])

_WRITE_ROLES = ("front_desk", "nurse", "system_admin")
_READ_ROLES = ("front_desk", "nurse", "doctor", "system_admin")


@router.post("/check-in")
async def check_in(
    payload: QueueCheckInRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> QueueTokenResponse:
    try:
        token = await queue_service.check_in(
            db,
            current_user,
            QueueCheckInPayload(
                appointment_id=payload.appointment_id,
                branch_id=payload.branch_id,
                department_id=payload.department_id,
            ),
        )
    except AppointmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return QueueTokenResponse.model_validate(token)


@router.patch("/{token_id}/status")
async def update_status(
    token_id: uuid.UUID,
    payload: StatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_WRITE_ROLES)),
) -> QueueTokenResponse:
    try:
        token = await queue_service.update_status(db, current_user, token_id, TokenStatus(payload.status))
    except QueueTokenNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except IllegalTokenStatusTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return QueueTokenResponse.model_validate(token)


@router.get("/board")
def get_board(
    branch_id: uuid.UUID,
    department_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> list[QueueTokenResponse]:
    tokens = queue_service.list_queue(db, current_user, branch_id, department_id)
    return [QueueTokenResponse.model_validate(token) for token in tokens]
