"""Notification & Alert Hub module (PRPs/notification-hub-prp.md, Phase 2).
Business logic lives in services/notification_service.py -- this module is
request validation, role gating, exception-to-status-code mapping only,
matching the pattern in routers/billing.py / routers/wards.py.

Per the PRP's ENDPOINTS table: `GET /notifications` is front_desk (view-only)
+ system_admin; `PATCH /notifications/{id}/retry` is system_admin only (a
manual retry is an operational action, not a front-desk one). Fine-grained
branch scoping is enforced inside notification_service.py via authorize()
(the tenant guard), not here -- require_role below is only the coarse "which
roles may call this endpoint at all" gate, same split billing.py/wards.py use.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.notification import NotificationResponse
from app.services import notification_service
from app.services.notification_service import NotificationNotFailedError

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])

_READ_ROLES = ("front_desk", "system_admin")
_RETRY_ROLES = ("system_admin",)


@router.get("")
def list_notifications(
    patient_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    status_param: str | None = Query(default=None, alias="status"),
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_READ_ROLES)),
) -> list[NotificationResponse]:
    return notification_service.list_notifications(
        db,
        current_user,
        patient_id=patient_id,
        user_id=user_id,
        status_param=status_param,
        branch_id_param=branch_id,
    )


@router.patch("/{notification_id}/retry")
def retry_notification(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_RETRY_ROLES)),
) -> NotificationResponse:
    try:
        return notification_service.retry_notification(db, current_user, notification_id)
    except NotificationNotFailedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
