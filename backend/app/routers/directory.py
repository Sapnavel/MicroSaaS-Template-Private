"""Generic, low-sensitivity reference-data lookup endpoints -- flat
list/lookup GETs that exist purely to populate frontend dropdowns that
previously required the caller to type a raw UUID/ID by hand (branch,
specialty, doctor, room, drug, staff). Every endpoint here is read-only;
business logic/query construction lives in services/directory_service.py,
this module stays thin (request validation + role gating only), matching
the pattern in routers/queue.py / routers/wards.py.

`GET /branches` / `GET /specialties` / `GET /drugs` are global reference
data with no branch scoping at all -- gated only by the coarse
`_ANY_STAFF_ROLE` require_role check, no `authorize()` call (there is
nothing to tenant-scope). `GET /doctors` / `GET /rooms` ARE branch-scoped --
services/directory_service.py calls `authorize()` against a `_BranchScoped`
stand-in for these, same "tenant guard is what actually enforces branch
isolation, the read policies registered in core/security.py only avoid
authorize()'s default-deny" split every other branch-scoped module in this
codebase uses.

`GET /staff` is `system_admin`-only -- a staff directory (names + roles) is
more sensitive than plain branch/specialty/doctor/room/drug reference data,
matching routers/admin.py's `POST /staff` role restriction.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.directory import (
    BranchListItem,
    DoctorListItem,
    DrugListItem,
    RoomListItem,
    SpecialtyListItem,
    StaffListItem,
)
from app.services import directory_service

router = APIRouter(prefix="/api/v1", tags=["directory"])

_ANY_STAFF_ROLE = ("doctor", "nurse", "front_desk", "lab_tech", "pharmacist", "billing_admin", "system_admin")

# `GET /branches`/`GET /specialties` are also reachable by role=patient
# (unlike every other endpoint in this router): both are global,
# non-sensitive reference data (branch/specialty names) with no
# `authorize()` tenant-guard call at all (see directory_service.py's module
# docstring), and a patient self-booking their own appointment via
# `POST /api/v1/me/appointments` needs exactly this data to choose a branch/
# specialty before searching doctors (`GET /api/v1/me/doctors`). Doctor/room/
# staff/drug lookups stay staff-only: those either carry PII (staff) or are
# only meaningful to someone already operating inside a specific branch's
# clinical workflow.
_ANY_AUTHENTICATED_ROLE = (*_ANY_STAFF_ROLE, "patient")


@router.get("/branches")
def list_branches(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANY_AUTHENTICATED_ROLE)),
) -> list[BranchListItem]:
    return directory_service.list_branches(db)


@router.get("/specialties")
def list_specialties(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANY_AUTHENTICATED_ROLE)),
) -> list[SpecialtyListItem]:
    return directory_service.list_specialties(db)


@router.get("/doctors")
def list_doctors(
    branch_id: uuid.UUID,
    specialty_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANY_STAFF_ROLE)),
) -> list[DoctorListItem]:
    return directory_service.list_doctors(db, current_user, branch_id, specialty_id)


@router.get("/rooms")
def list_rooms(
    branch_id: uuid.UUID,
    room_type: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANY_STAFF_ROLE)),
) -> list[RoomListItem]:
    return directory_service.list_rooms(db, current_user, branch_id, room_type)


@router.get("/drugs")
def list_drugs(
    query: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANY_STAFF_ROLE)),
) -> list[DrugListItem]:
    return directory_service.list_drugs(db, query)


@router.get("/staff")
def list_staff(
    branch_id: uuid.UUID,
    role: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("system_admin")),
) -> list[StaffListItem]:
    users = directory_service.list_staff(db, branch_id, role)
    return [StaffListItem(id=u.id, full_name=u.full_name, role=u.role.value, branch_id=u.branch_id) for u in users]
