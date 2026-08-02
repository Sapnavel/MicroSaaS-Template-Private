"""Pydantic request/response schemas for the auth and admin-staff endpoints.

Field names and casing here are a contract shared with FRONTEND-AGENT (see
PRPs/auth-rbac-abac-prp.md, ENDPOINTS table) — do not rename fields without
updating the frontend in lockstep.

Note: email fields are typed `str`, not `pydantic.EmailStr`. `email-validator`
(the package `EmailStr` requires) is not in backend/requirements.txt and no
other schema in this codebase uses it; adding it here would be an
undeclared new dependency. Format validation for email is a UX nicety, not
a security control (authenticate_user() in services/auth_service.py doesn't
care whether the string looks like an email), so this is a safe deviation.
Flagged in the phase report.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    """Patient self-registration. `extra="forbid"` is deliberate: the API
    contract requires rejecting (422) any request that includes a `role` or
    `branch_id` field at all, matching or not — role/branch are never
    client-settable on this endpoint (see routers/auth.py `register`,
    services/auth_service.py `register_patient`)."""

    model_config = ConfigDict(extra="forbid")

    email: str
    # max_length=72: bcrypt (passlib backend) either silently truncates or
    # raises past 72 bytes depending on version — bound it here so an
    # over-length password is a clean 422, not an unhandled 500 from
    # hash_password() (REVIEW-AGENT finding M3).
    password: str = Field(min_length=8, max_length=72)
    full_name: str


class RegisterResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str

    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    """Shared shape for both /auth/login (staff) and /auth/patient/login."""

    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    branch_id: UUID | None = None
    hospital_group_id: UUID | None = None
    is_active: bool
    is_verified: bool

    model_config = ConfigDict(from_attributes=True)


class StaffProvisionRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=72)
    full_name: str
    role: str
    branch_id: UUID


class StaffProfile(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    branch_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)
