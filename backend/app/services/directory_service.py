"""Business logic for routers/directory.py's generic reference-data lookup
endpoints. Routers stay thin (request validation + role gating only); every
query and response-shaping decision lives here, same "business logic lives
in services/, router is thin" split as services/queue_service.py /
services/ward_service.py.

TENANT SCOPING: `list_branches`/`list_specialties`/`list_drugs` are global
reference data -- `Branch`, `Specialty`, and `Drug` carry no `branch_id`
column at all (confirmed by reading models/tenant.py, models/resource.py,
models/consultation.py), so no `authorize()` tenant-guard call is made for
these; there is nothing to scope. `list_doctors`/`list_rooms` ARE
branch-scoped (`Doctor.branch_id`/`Room.branch_id` are real columns) --
`_BranchScoped` mirrors `queue_service._BranchScoped` /
`pharmacy_service._BranchScoped` / `ward_service._BranchScoped` exactly: a
minimal stand-in resource for `authorize()`'s tenant guard when there is no
single ORM row to check against (a branch-wide "list every doctor/room in
this branch" read). The tenant guard inside `authorize()` is what actually
enforces branch isolation here; the `"directory"`/`"read"` policies
registered for every staff role in core/security.py exist only so a role
that passes the tenant guard isn't then denied by authorize()'s "no policy
registered" default-deny (same reminder every prior module's own policy
block includes).

`list_staff` deliberately does NOT call `authorize()` at all: its router
(routers/directory.py) gates to `system_admin` only, and `system_admin`
already bypasses the tenant guard entirely (core/security.py's
`_admin_bypass`) -- an authorize() call here would be a costless no-op, not
genuine defense-in-depth, since no other role can ever reach this function.

`list_doctors`/`list_rooms` filter to `is_active=True` only -- these
endpoints exist to populate a frontend dropdown for NEW work (e.g. "which
doctor is this appointment for"), and a deactivated doctor/room should not be
selectable there, even though it may still be referenced by historical rows
elsewhere. A judgment call, not spelled out by the task brief.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.security import authorize
from app.models.consultation import Drug
from app.models.resource import Doctor, Room, Specialty
from app.models.tenant import Branch
from app.models.user import User, UserRole
from app.schemas.directory import (
    BranchListItem,
    DoctorListItem,
    DrugListItem,
    RoomListItem,
    SpecialtyListItem,
)

_DRUG_LIST_LIMIT = 100


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard -- see
    module docstring. Same pattern as every other branch-scoped module's own
    `_BranchScoped`."""

    branch_id: uuid.UUID


def list_branches(db: Session) -> list[BranchListItem]:
    """GET /api/v1/branches -- global reference data, no tenant scoping."""
    branches = db.execute(select(Branch).order_by(Branch.name)).scalars().all()
    return [BranchListItem.model_validate(b) for b in branches]


def list_specialties(db: Session) -> list[SpecialtyListItem]:
    """GET /api/v1/specialties -- global reference data, no tenant scoping."""
    specialties = db.execute(select(Specialty).order_by(Specialty.name)).scalars().all()
    return [SpecialtyListItem.model_validate(s) for s in specialties]


def list_doctors(
    db: Session, current_user: User, branch_id: uuid.UUID, specialty_id: int | None
) -> list[DoctorListItem]:
    """GET /api/v1/doctors?branch_id=&specialty_id="""
    authorize(current_user, "directory", "read", _BranchScoped(branch_id=branch_id))

    query = (
        select(Doctor.id, User.full_name, Doctor.specialty_id, Doctor.branch_id)
        .join(User, User.id == Doctor.user_id)
        .where(Doctor.branch_id == branch_id, Doctor.is_active.is_(True))
    )
    if specialty_id is not None:
        query = query.where(Doctor.specialty_id == specialty_id)
    query = query.order_by(User.full_name)

    rows = db.execute(query).all()
    return [DoctorListItem(id=row[0], full_name=row[1], specialty_id=row[2], branch_id=row[3]) for row in rows]


def list_rooms(db: Session, current_user: User, branch_id: uuid.UUID, room_type: str | None) -> list[RoomListItem]:
    """GET /api/v1/rooms?branch_id=&room_type="""
    authorize(current_user, "directory", "read", _BranchScoped(branch_id=branch_id))

    query = select(Room).where(Room.branch_id == branch_id, Room.is_active.is_(True))
    if room_type is not None:
        query = query.where(Room.room_type == room_type)
    query = query.order_by(Room.name)

    rooms = db.execute(query).scalars().all()
    return [RoomListItem.model_validate(r) for r in rooms]


def list_drugs(db: Session, query_param: str | None) -> list[DrugListItem]:
    """GET /api/v1/drugs?query= -- searches the ~294-row seeded `Drug`
    reference table by name/generic_name (case-insensitive partial match)
    when `query` is given, else returns the first `_DRUG_LIST_LIMIT` rows
    alphabetically. Global reference data -- no tenant scoping (see module
    docstring).

    `_DRUG_LIST_LIMIT` only applies to the unfiltered "browse everything"
    case. A `query`-filtered search is already narrowed by the caller's own
    input, so it is not capped -- capping it too would let an unrelated,
    alphabetically-earlier match crowd out the very row the caller searched
    for (hit in practice: this table is deliberately never truncated between
    tests, so a long-lived dev/test database accumulates many throwaway
    fixture-created drugs over time, some of which can outnumber a capped
    limit for a broad substring pattern)."""
    query_stmt = select(Drug)
    if query_param:
        pattern = f"%{query_param}%"
        query_stmt = query_stmt.where(or_(Drug.name.ilike(pattern), Drug.generic_name.ilike(pattern)))
        query_stmt = query_stmt.order_by(Drug.name)
    else:
        query_stmt = query_stmt.order_by(Drug.name).limit(_DRUG_LIST_LIMIT)

    drugs = db.execute(query_stmt).scalars().all()
    return [DrugListItem.model_validate(d) for d in drugs]


def list_staff(db: Session, branch_id: uuid.UUID, role_param: str | None) -> list[User]:
    """GET /api/v1/staff?branch_id=&role= -- router-gated to `system_admin`
    only, see module docstring for why no `authorize()` call is made here.
    Excludes `role=patient` unconditionally (this is a STAFF directory, not
    a general user list)."""
    role_filter: UserRole | None = None
    if role_param is not None:
        try:
            role_filter = UserRole(role_param)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown role: {role_param!r}") from exc
        if role_filter == UserRole.patient:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "role=patient is not a staff role")

    query = select(User).where(User.branch_id == branch_id, User.role != UserRole.patient)
    if role_filter is not None:
        query = query.where(User.role == role_filter)
    query = query.order_by(User.full_name)

    return list(db.execute(query).scalars().all())
