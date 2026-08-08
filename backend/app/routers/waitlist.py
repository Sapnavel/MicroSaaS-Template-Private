"""HMS Project Completion Prompt, section 3.3: "Waiting list" / "Fair slot
allocation" -- see services/waitlist_service.py's module docstring for the
FIFO fairness algorithm and why offering a freed slot never auto-books it.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import get_caller_branch_id, require_role
from app.database import get_db
from app.models.user import User
from app.schemas.appointment import WaitlistEntryResponse, WaitlistFulfillRequest, WaitlistJoinCreate
from app.services import waitlist_service

router = APIRouter(prefix="/api/v1/waitlist", tags=["waitlist"])

_STAFF_ROLES = ("front_desk", "system_admin", "doctor", "nurse")


def _resolve_branch_id(current_user: User) -> uuid.UUID:
    branch_id_str = get_caller_branch_id(current_user)
    if branch_id_str is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no branch assigned")
    return uuid.UUID(branch_id_str)


@router.post("", status_code=status.HTTP_201_CREATED)
def join_waitlist(
    payload: WaitlistJoinCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_STAFF_ROLES)),
) -> WaitlistEntryResponse:
    entry = waitlist_service.join_waitlist(db, current_user, _resolve_branch_id(current_user), payload)
    return WaitlistEntryResponse.model_validate(entry)


@router.get("")
def list_waitlist(
    doctor_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_STAFF_ROLES)),
) -> list[WaitlistEntryResponse]:
    entries = waitlist_service.list_waitlist(db, current_user, _resolve_branch_id(current_user), doctor_id)
    return [WaitlistEntryResponse.model_validate(e) for e in entries]


@router.post("/{entry_id}/fulfill")
def fulfill_waitlist_entry(
    entry_id: uuid.UUID,
    payload: WaitlistFulfillRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_STAFF_ROLES)),
) -> WaitlistEntryResponse:
    entry = waitlist_service.fulfill_waitlist_entry(db, current_user, entry_id, payload.appointment_id)
    return WaitlistEntryResponse.model_validate(entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_waitlist_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_STAFF_ROLES)),
) -> None:
    waitlist_service.cancel_waitlist_entry(db, current_user, entry_id)
