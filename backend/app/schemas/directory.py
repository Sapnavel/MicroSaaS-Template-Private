"""Pydantic response schemas for generic, low-sensitivity reference-data
lookup endpoints (routers/directory.py). These exist purely to populate
frontend dropdowns that previously required a user to type a raw UUID/ID by
hand (branch, specialty, doctor, room, drug, staff) -- see the "frontend
UUID-to-dropdown conversion" follow-up phase this backend work feeds.

Field names/casing here are a contract shared with FRONTEND-AGENT, working in
parallel against the same conversion effort -- do not rename fields without
updating the frontend in lockstep. Plain snake_case, no camelCase aliasing,
same convention every other schema module in this codebase uses (the
frontend converts to camelCase at its own service-layer boundary).
"""

import uuid

from pydantic import BaseModel, ConfigDict


class BranchListItem(BaseModel):
    """GET /api/v1/branches response line."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class SpecialtyListItem(BaseModel):
    """GET /api/v1/specialties response line."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class DoctorListItem(BaseModel):
    """GET /api/v1/doctors response line. Built by hand in
    services/directory_service.py from a `Doctor` joined to its `User` (not
    plain `from_attributes` off a `Doctor` row alone -- `full_name` lives on
    `User`, not `Doctor`)."""

    id: uuid.UUID
    full_name: str
    specialty_id: int
    branch_id: uuid.UUID


class RoomListItem(BaseModel):
    """GET /api/v1/rooms response line."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    room_type: str
    branch_id: uuid.UUID


class DrugListItem(BaseModel):
    """GET /api/v1/drugs response line."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    generic_name: str | None


class StaffListItem(BaseModel):
    """GET /api/v1/staff response line. Built by hand in
    routers/directory.py from a `User` row (`role` is an enum member there,
    serialized here as its plain `.value` string -- mirrors
    `schemas/queue.py`'s `QueueTokenResponse.status` convention)."""

    id: uuid.UUID
    full_name: str
    role: str
    branch_id: uuid.UUID | None
