"""Notification & Alert Hub module HTTP-facing service layer
(PRPs/notification-hub-prp.md, Phase 2). Router (routers/notifications.py)
stays thin and delegates here -- same "business logic lives in services/,
router is thin" split as services/ward_service.py / services/billing_service.py.

This module is deliberately separate from services/notification_engine.py:
`notification_engine.handle_event` is the RabbitMQ consumer's entry point
(Phase 1, already reviewed/merged, imported from here but never duplicated).
This module is the HTTP-facing read/retry surface a staff member uses
through the router -- a different caller, a different concern.

`_BranchScoped` and `_resolve_branch_filter` mirror
`billing_service._BranchScoped` / `billing_service._resolve_branch_filter`
exactly: a minimal stand-in resource for `authorize()`'s tenant guard when
there is no single ORM row to check against yet (the list endpoint, which
aggregates across many rows rather than one). Every branch resolution here
goes through `core.security.get_caller_branch_id`, never
`current_user.branch_id` directly -- see that function's own docstring
for why the two answers can legitimately disagree mid-session (REVIEW-AGENT
finding H1, PRPs/pharmacy-module-prp.md Phase 3).

`retry_notification` authorizes directly against the loaded `Notification`
row (it has a real `branch_id` column already, same as `billing_service.get_invoice`
authorizing directly against a loaded `Invoice` -- no `_BranchScoped`
stand-in needed there either).
"""

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.notification_providers import get_provider
from app.core.security import authorize, get_caller_branch_id
from app.models.notification import Notification, NotificationStatus
from app.models.patient import Patient
from app.models.user import User, UserRole
from app.schemas.notification import NotificationResponse

logger = logging.getLogger(__name__)


class NotificationNotFailedError(Exception):
    """Raised by `retry_notification` when the target row's `status` is not
    `"failed"` -- only a failed send is eligible for a manual retry (per the
    PRP's design decision #2: there is no automatic retry/backoff, a staff
    member manually retries a failed row, and only a failed row). Router
    maps this to 409."""


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard when there
    is no single ORM row to check against -- the list endpoint, which
    aggregates across many rows rather than one. Same pattern as
    `billing_service._BranchScoped`/`ward_service._BranchScoped`."""

    branch_id: uuid.UUID


def _resolve_branch_filter(current_user: User, branch_id_param: uuid.UUID | None) -> uuid.UUID | None:
    """`system_admin` may omit `branch_id` to mean "all branches"; every
    other role MUST supply `branch_id` and it MUST match their own
    (token-claims-preferred, via `get_caller_branch_id` -- never
    `current_user.branch_id` directly). Mismatch or omission for a
    non-admin caller is 422 -- mirrors `billing_service._resolve_branch_filter`
    exactly, do not reinvent a looser version."""
    if current_user.role == UserRole.system_admin:
        return branch_id_param

    caller_branch_id = get_caller_branch_id(current_user)
    if caller_branch_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "your account has no branch assigned; contact an administrator",
        )
    caller_branch_uuid = uuid.UUID(caller_branch_id)
    if branch_id_param is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "branch_id is required")
    if branch_id_param != caller_branch_uuid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "branch_id in the request does not match your assigned branch",
        )
    return caller_branch_uuid


def _get_notification_or_404(db: Session, notification_id: uuid.UUID) -> Notification:
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"notification {notification_id} not found")
    return notification


def list_notifications(
    db: Session,
    current_user: User,
    *,
    patient_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    status_param: str | None = None,
    branch_id_param: uuid.UUID | None = None,
) -> list[NotificationResponse]:
    """GET /api/v1/notifications. Resolves the effective branch filter,
    authorizes against a `_BranchScoped` stand-in when a concrete branch_id
    is resolved (system_admin querying "all branches" skips this -- the
    tenant guard inside `authorize()` already bypasses for system_admin, and
    no other role can reach this branch since a non-admin always resolves to
    a concrete branch_id above), then queries `Notification` filtered by
    whichever of patient_id/user_id/status/branch_id were supplied,
    combined with AND."""
    branch_id = _resolve_branch_filter(current_user, branch_id_param)
    if branch_id is not None:
        authorize(current_user, "notification", "read", _BranchScoped(branch_id=branch_id))

    query = select(Notification)
    if patient_id is not None:
        query = query.where(Notification.patient_id == patient_id)
    if user_id is not None:
        query = query.where(Notification.user_id == user_id)
    if status_param is not None:
        query = query.where(Notification.status == status_param)
    if branch_id is not None:
        query = query.where(Notification.branch_id == branch_id)

    notifications = db.execute(query).scalars().all()
    return [NotificationResponse.model_validate(notification) for notification in notifications]


def retry_notification(db: Session, current_user: User, notification_id: uuid.UUID) -> NotificationResponse:
    """PATCH /api/v1/notifications/{id}/retry. Authorizes directly against
    the loaded `Notification` row (a real `branch_id` column already, no
    `_BranchScoped` stand-in needed -- mirrors `billing_service.get_invoice`).
    Raises `NotificationNotFailedError` (-> 409 at the router) unless the
    row's `status` is currently `"failed"`.

    Re-resolves the recipient string with the same two-branch lookup
    `notification_engine._resolve_recipient_string` uses (patient_id ->
    `Patient.phone`, else user_id -> `User.email`) and re-attempts the
    provider send via `get_provider(channel).send(...)`, wrapped in the same
    try/except-turns-into-failed discipline `handle_event` uses so a second
    failed retry can never crash this endpoint. Updates `status` to
    `"sent"`/`"failed"` accordingly, commits, and returns the row.
    """
    notification = _get_notification_or_404(db, notification_id)
    authorize(current_user, "notification", "write", notification)

    if notification.status != NotificationStatus.failed.value:
        raise NotificationNotFailedError(
            f"notification {notification_id} has status={notification.status!r}, only a "
            f"{NotificationStatus.failed.value!r} notification can be retried"
        )

    if notification.patient_id is not None:
        patient = db.get(Patient, notification.patient_id)
        recipient = patient.phone if patient is not None else None
    elif notification.user_id is not None:
        user = db.get(User, notification.user_id)
        recipient = user.email if user is not None else None
    else:
        recipient = None

    if recipient is None:
        # No phone/email to send to at all -- never a "successful send",
        # regardless of what a stub provider would otherwise return.
        succeeded = False
    else:
        try:
            succeeded = get_provider(notification.channel).send(
                channel=notification.channel,
                recipient=recipient,
                template=notification.template,
                payload=notification.payload,
            )
        except Exception:
            logger.exception(
                "notification retry failed for notification_id=%s channel=%s template=%s",
                notification.id,
                notification.channel,
                notification.template,
            )
            succeeded = False

    notification.status = NotificationStatus.sent.value if succeeded else NotificationStatus.failed.value
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return NotificationResponse.model_validate(notification)
