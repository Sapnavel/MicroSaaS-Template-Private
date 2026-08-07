"""JWT auth + RBAC/ABAC policy engine.

RBAC answers "can this role ever do X". ABAC answers "can *this* user do X
*on this specific resource instance*". We compose them: a Policy is
registered per (role, resource_type, action) and receives the current user
plus the resource row, so ownership/coverage checks live in one place
instead of being copy-pasted into every router.

Token signing uses a `kid`-based keyring (`settings.jwt_signing_keys`) so
signing keys can be rotated without invalidating tokens issued under a
still-valid previous key — see PRPs/auth-rbac-abac-prp.md, Security Design
section 1, for the rotation procedure.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import redis
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, set_tenant_context
from app.models.resource import Doctor
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# Same Redis client construction pattern as core/locking.py, used here for
# the access-token revocation blocklist (Security Design section 4).
_redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# Token issuance / verification
# ---------------------------------------------------------------------------


def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    role: str | None,
    branch_id: str | None,
    hospital_group_id: str | None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "jti": str(uuid.uuid4()),
        "type": token_type,
        "role": role,
        "branch_id": branch_id,
        "hospital_group_id": hospital_group_id,
        "iat": now,
        "exp": now + expires_delta,
    }
    signing_key = settings.jwt_signing_keys[settings.jwt_current_kid]
    return jwt.encode(
        payload,
        signing_key,
        algorithm=settings.algorithm,
        headers={"kid": settings.jwt_current_kid},
    )


def create_access_token(
    subject: str,
    role: str | None = None,
    branch_id: str | None = None,
    hospital_group_id: str | None = None,
) -> str:
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        role=role,
        branch_id=branch_id,
        hospital_group_id=hospital_group_id,
    )


def create_refresh_token(
    subject: str,
    role: str | None = None,
    branch_id: str | None = None,
    hospital_group_id: str | None = None,
) -> str:
    return _create_token(
        subject,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
        role=role,
        branch_id=branch_id,
        hospital_group_id=hospital_group_id,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT using the signing keyring.

    The `kid` in the (unverified) header selects which key in
    `settings.jwt_signing_keys` to verify against, so tokens signed under a
    previous `kid` keep verifying as long as that key remains in the
    keyring (rotation overlap window). An unrecognized `kid` is a 401, not
    a fallback to any other key.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token header") from exc

    kid = unverified_header.get("kid")
    signing_key = settings.jwt_signing_keys.get(kid) if kid else None
    if signing_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown signing key")

    try:
        return jwt.decode(token, signing_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc


def is_token_blocklisted(jti: str) -> bool:
    """Public (whole-system review finding H1, WebSocket auth): the queue
    board WebSocket endpoint (websocket/queue_board.py) needs the same
    revocation check `get_current_user` runs, but can't use
    `get_current_user` directly -- it's a `Depends(oauth2_scheme)` REST
    dependency expecting an `Authorization` header, and browsers can't set
    custom headers on a WebSocket upgrade request, so that endpoint reads
    the token from a query param instead and validates it manually. Rather
    than reach into the module-private `_redis_client` from another file,
    this small wrapper is the reuse seam -- same rationale
    `get_caller_branch_id` gives for being public instead of internal-only.
    """
    return bool(_redis_client.exists(f"blocklist:{jti}"))


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")

    # Revocation check happens BEFORE the DB round-trip: a blocklisted jti
    # should never even cause a user lookup (Security Design section 4).
    jti = payload.get("jti")
    if jti and is_token_blocklisted(jti):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token has been revoked")

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or inactive")

    # Stash the validated token claims on the user instance. `authorize()`
    # prefers these over a fresh DB read for tenant scoping: if an admin
    # moves this user to a different branch mid-session, the still-live
    # token must keep enforcing the *old* branch_id rather than silently
    # picking up the new one — the user must re-login to get a token with
    # updated claims (Security Design section 3). This is a plain instance
    # attribute, not a mapped column, so it is never persisted.
    user.token_claims = payload  # type: ignore[attr-defined]

    # Stash the caller's Doctor.id (a `doctors` row PK, distinct from
    # `users.id`) when the caller is a doctor. `Consultation.doctor_id` (and
    # `Appointment.doctor_id`) reference `doctors.id`, NOT `users.id` -- a
    # policy that compared `consultation.doctor_id == user.id` would compare
    # two different id spaces and never match a real doctor to their own
    # consultation (found while building the Clinical Consultation module,
    # PRPs/clinical-consultation-prescription-prp.md, before any policy
    # actually depended on it working). One extra query per request, only
    # for doctor-role callers -- same class of per-request cost already
    # accepted for the blocklist check above.
    user.doctor_id = None  # type: ignore[attr-defined]
    if user.role == UserRole.doctor:
        doctor = db.execute(select(Doctor.id).where(Doctor.user_id == user.id)).scalar_one_or_none()
        user.doctor_id = doctor  # type: ignore[attr-defined]

    # Defense-in-depth: stamp the RLS session var for the rest of this request's
    # transactions. system_admin (branch_id None) bypasses branch RLS by design.
    tenant_branch_id = payload.get("branch_id") or (str(user.branch_id) if user.branch_id else None)
    if tenant_branch_id:
        set_tenant_context(db, tenant_branch_id)
    return user


def require_role(*allowed_roles: str) -> Callable[[User], User]:
    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return _check


# ---------------------------------------------------------------------------
# ABAC policy registry
# ---------------------------------------------------------------------------

PolicyFn = Callable[[User, Any], bool]
_policies: dict[tuple[str, str, str], PolicyFn] = {}


def policy(role: str, resource_type: str, action: str) -> Callable[[PolicyFn], PolicyFn]:
    """Decorator to register an ABAC policy for (role, resource_type, action)."""

    def _register(fn: PolicyFn) -> PolicyFn:
        _policies[(role, resource_type, action)] = fn
        return fn

    return _register


def get_caller_branch_id(user: User) -> str | None:
    """Prefer the branch_id captured in the validated JWT over a fresh DB
    read (see the comment in `get_current_user`). Falls back to the DB
    value when no token claims are attached, e.g. in unit tests that
    construct a `User` directly without going through `get_current_user`.

    Public (no leading underscore, unlike when this only backed
    `authorize()`'s tenant guard internally): REVIEW-AGENT finding H1
    (PRPs/pharmacy-module-prp.md Phase 3) -- the Pharmacy module's own
    branch-resolution helpers (`pharmacy_service._resolve_branch_id`/
    `_resolve_branch_filter`) were reading `current_user.branch_id` directly
    (a live DB value), while `authorize()`'s tenant guard compared against
    this function's token-frozen value -- two different answers to "what is
    the caller's branch" inside the same request, disagreeing specifically
    in the case this function's docstring exists to handle (an admin
    reassigns a user's branch mid-session; the old token must keep
    enforcing the *old* branch until re-login). Any module doing its own
    branch resolution should call this, not read `user.branch_id` directly,
    so there is exactly one authoritative answer.
    """
    claims: dict[str, Any] | None = getattr(user, "token_claims", None)
    if claims is not None:
        branch_id = claims.get("branch_id")
        return str(branch_id) if branch_id else None
    return str(user.branch_id) if user.branch_id else None


def authorize(user: User, resource_type: str, action: str, resource: Any) -> None:
    """Raise 403 unless this caller may perform `action` on `resource`.

    Two checks run in order:
    1. Tenant guard (default-deny, not opt-in): if `resource` has a
       `branch_id` attribute, it must equal the caller's branch_id, unless
       the caller is `system_admin` (cross-branch by design). This runs
       BEFORE any registered policy so a missing or buggy policy can never
       accidentally leak cross-branch data — a resource with a mismatched
       (or unresolvable) branch_id is denied here, not silently skipped.
    2. The registered ABAC policy for (role, resource_type, action), as
       before. Roles with no registered policy are denied by default.
    """
    if hasattr(resource, "branch_id") and user.role != UserRole.system_admin:
        caller_branch_id = get_caller_branch_id(user)
        resource_branch_id = resource.branch_id
        if caller_branch_id is None or str(resource_branch_id) != caller_branch_id:
            logger.warning(
                "tenant guard denied user=%s role=%s resource_type=%s action=%s",
                getattr(user, "id", None),
                user.role,
                resource_type,
                action,
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-branch access denied")

    fn = _policies.get((user.role, resource_type, action)) or _policies.get((user.role, "*", "*"))
    if fn is None or not fn(user, resource):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not authorized for this resource")


# Example built-in policies (extend per module as routers are implemented):


def _caller_doctor_id(user: User) -> str | None:
    """`get_current_user` stashes `doctor_id` (a `doctors.id`, distinct from
    `users.id`) on the user instance for doctor-role callers -- see its
    docstring. Falls back to `None` (never matches) if unset, e.g. a bare
    test double built without going through `get_current_user`."""
    doctor_id = getattr(user, "doctor_id", None)
    return str(doctor_id) if doctor_id else None


@policy("doctor", "consultation", "read")
def _doctor_reads_own_consultations(user: User, consultation: Any) -> bool:
    """`consultation.doctor_id` references `doctors.id`, NOT `users.id` --
    comparing against `user.id` directly (the original placeholder version
    of this policy) would never match a real doctor to their own
    consultation. See `_caller_doctor_id` / `get_current_user`."""
    return _caller_doctor_id(user) == str(consultation.doctor_id)


@policy("doctor", "consultation", "write")
def _doctor_writes_own_consultations(user: User, consultation: Any) -> bool:
    """Same ownership rule as the read policy, for actions that mutate a
    consultation (complete it, add a diagnosis, write a prescription) --
    Clinical Consultation module (PRPs/clinical-consultation-prescription-prp.md)."""
    return _caller_doctor_id(user) == str(consultation.doctor_id)


@policy("nurse", "consultation", "read")
def _nurse_reads_any_consultation(user: User, consultation: Any) -> bool:
    # Nurses read any consultation (care coordination), not scoped to a
    # single doctor's patients -- per the Clinical Consultation module's
    # ENDPOINTS table. No write policy for nurse/consultation: nurses don't
    # author consultations, diagnoses, or prescriptions in this module.
    return True


@policy("front_desk", "patient", "read")
def _front_desk_reads_demographics_only(user: User, patient: Any) -> bool:
    # Router is responsible for excluding clinical fields from the response
    # schema for this role; this policy only gates row-level access.
    return True


# PRPs/patient-master-index-prp.md Phase 2: `patients` has no `branch_id`
# (branch-agnostic by design -- see services/patient_matching.py's module
# docstring), so the tenant guard in `authorize()` never fires for this
# resource_type. That means any role reaching a patient-read call site is
# gated *only* by whether a policy is registered here at all -- authorize()
# denies by default, so doctor/nurse would 403 on every patient read without
# an explicit registration, even though the ENDPOINTS table in the PRP
# clearly intends them to have full-record read access. system_admin needs
# no separate entry: `_admin_bypass` (role="system_admin", resource_type="*",
# action="*") already covers it, so adding a redundant
# `@policy("system_admin", "patient", "read")` here would just shadow that
# wildcard for no benefit.
@policy("doctor", "patient", "read")
def _doctor_reads_full_patient_record(user: User, patient: Any) -> bool:
    # Clinical role: response shaping (services/patient_service.py's
    # shape_patient_response) gives this role the full record, including
    # allergies -- this policy only gates row-level access, same split as
    # _front_desk_reads_demographics_only above.
    return True


@policy("nurse", "patient", "read")
def _nurse_reads_full_patient_record(user: User, patient: Any) -> bool:
    return True


@policy("system_admin", "*", "*")
def _admin_bypass(user: User, resource: Any) -> bool:
    return True


# PRPs/pharmacy-module-prp.md Phase 2: `inventory_item`/`inventory_batch` are
# the first resources in this build sequence where the tenant guard above
# actually restricts access for real (see models/pharmacy.py's module
# docstring) -- a pharmacist at Branch A must never read/write Branch B's
# stock. The tenant guard runs BEFORE these policies and is what actually
# enforces that; these two policies exist only so `authorize()` doesn't
# deny-by-default once the tenant guard has already passed (same reminder
# every prior module's PRP has included). `system_admin` needs no separate
# entry here: `_admin_bypass` above already covers it.
@policy("pharmacist", "inventory_item", "read")
def _pharmacist_reads_own_branch_inventory(user: User, resource: Any) -> bool:
    return True


@policy("pharmacist", "inventory_item", "write")
def _pharmacist_writes_own_branch_inventory(user: User, resource: Any) -> bool:
    return True


# PRPs/ward-bed-ot-module-prp.md Phase 2: `ward` is the resource_type used
# for BOTH the ward/bed/admission surface (resource passed is the `Ward` ORM
# row, resolved by services/ward_service.py by walking Bed -> Ward) AND OT
# scheduling (resource passed is the `Room` ORM row directly, since
# `Room.branch_id` is already a real column -- an OT room is still a
# ward-module concern, see ward_service.py's module docstring for why a
# second resource_type was rejected). As with Pharmacy's
# inventory_item policies above, the tenant guard inside `authorize()` is
# what actually enforces branch isolation here (a nurse/doctor at Branch A
# must never act on Branch B's beds or OT rooms) -- these policies exist
# only so a role that passes the tenant guard isn't then denied by
# authorize()'s "no policy registered" default-deny. `front_desk` gets a
# read policy only: per the ENDPOINTS table, front_desk only ever reads the
# live bed matrix, it never admits/discharges/transfers/schedules OT.
# `system_admin` needs no separate entry: `_admin_bypass` above already
# covers it.
@policy("nurse", "ward", "read")
def _nurse_reads_own_branch_ward(user: User, resource: Any) -> bool:
    return True


@policy("nurse", "ward", "write")
def _nurse_writes_own_branch_ward(user: User, resource: Any) -> bool:
    return True


@policy("doctor", "ward", "read")
def _doctor_reads_own_branch_ward(user: User, resource: Any) -> bool:
    return True


@policy("doctor", "ward", "write")
def _doctor_writes_own_branch_ward(user: User, resource: Any) -> bool:
    return True


@policy("front_desk", "ward", "read")
def _front_desk_reads_own_branch_ward(user: User, resource: Any) -> bool:
    return True


# PRPs/billing-module-prp.md Phase 2: `billing` is the resource_type used for
# `Invoice`/`InvoiceItem`/`InsuranceClaim` (the latter two scoped
# transitively through `invoice_id -> invoices.branch_id`, resolved by
# services/billing_service.py before authorize() is ever called -- same
# "no branch_id column of its own" pattern as `beds`/`admissions` under
# `wards`). As with Ward/Pharmacy's own policies above, the tenant guard
# inside `authorize()` is what actually enforces branch isolation here (a
# billing_admin/front_desk at Branch A must never see/touch Branch B's
# invoices) -- these policies exist only so a role that passes the tenant
# guard isn't then denied by authorize()'s "no policy registered"
# default-deny. `front_desk` gets a read policy only: per the ENDPOINTS
# table, front_desk only ever reads invoices (checkout needs to see balance
# due), it never creates/mutates billing state. `system_admin` needs no
# separate entry: `_admin_bypass` above already covers it.
@policy("billing_admin", "billing", "read")
def _billing_admin_reads_own_branch_billing(user: User, resource: Any) -> bool:
    return True


@policy("billing_admin", "billing", "write")
def _billing_admin_writes_own_branch_billing(user: User, resource: Any) -> bool:
    return True


@policy("front_desk", "billing", "read")
def _front_desk_reads_own_branch_billing(user: User, resource: Any) -> bool:
    return True


# PRPs/notification-hub-prp.md Phase 2: `notification` is the resource_type
# used for `Notification` rows (a real `branch_id` column of its own, same as
# `Invoice` -- see services/notification_service.py's module docstring). The
# tenant guard inside authorize() is what actually enforces branch isolation
# here; this policy exists only so front_desk isn't then denied by
# authorize()'s "no policy registered" default-deny. front_desk gets a read
# policy only, per the PRP's ENDPOINTS table (view-only; manual retry is
# system_admin only). `system_admin` needs no separate entry: `_admin_bypass`
# above already covers both read and write.
@policy("front_desk", "notification", "read")
def _front_desk_reads_own_branch_notifications(user: User, resource: Any) -> bool:
    return True


# PRPs/realtime-queue-prp.md Phase 2: `queue_token` is the resource_type used
# for `QueueToken` rows (a real `branch_id` column of its own -- see
# services/queue_service.py's module docstring) and for `list_queue`'s
# branch-wide read (a `_BranchScoped` stand-in carrying just `branch_id`,
# same pattern as Pharmacy's `_BranchScoped`). As with every prior
# branch-scoped module's own policies above, the tenant guard inside
# `authorize()` is what actually enforces branch isolation here (a
# front_desk/nurse/doctor at Branch A must never see/touch Branch B's queue)
# -- these policies exist only so a role that passes the tenant guard isn't
# then denied by authorize()'s "no policy registered" default-deny. `doctor`
# gets a read policy only, per the PRP's RBAC design decision #6: a doctor
# may glance at their own live queue but doesn't operate the front desk
# (check in patients, call/delay/skip tokens). `system_admin` needs no
# separate entry: `_admin_bypass` above already covers it.
@policy("front_desk", "queue_token", "read")
def _front_desk_reads_own_branch_queue(user: User, resource: Any) -> bool:
    return True


@policy("front_desk", "queue_token", "write")
def _front_desk_writes_own_branch_queue(user: User, resource: Any) -> bool:
    return True


@policy("nurse", "queue_token", "read")
def _nurse_reads_own_branch_queue(user: User, resource: Any) -> bool:
    return True


@policy("nurse", "queue_token", "write")
def _nurse_writes_own_branch_queue(user: User, resource: Any) -> bool:
    return True


@policy("doctor", "queue_token", "read")
def _doctor_reads_own_branch_queue(user: User, resource: Any) -> bool:
    return True


# Frontend UUID-to-dropdown conversion follow-up (backend phase): `directory`
# is the resource_type used for the branch-scoped reference-data lookups in
# routers/directory.py (`GET /doctors`, `GET /rooms` -- both pass a
# `directory_service._BranchScoped` stand-in carrying just `branch_id`, same
# pattern every other branch-scoped module's own `_BranchScoped` uses).
# `GET /branches`/`GET /specialties`/`GET /drugs` never call `authorize()` at
# all (no `branch_id` column on those tables to scope -- see
# directory_service.py's module docstring), so they need no policy here.
# `GET /staff` is `system_admin`-only at the router (`require_role`) and
# never calls `authorize()` either (see directory_service.list_staff's
# docstring). As with every prior branch-scoped module's own policies above,
# the tenant guard inside `authorize()` is what actually enforces branch
# isolation for `GET /doctors`/`GET /rooms` -- these policies exist only so a
# role that passes the tenant guard isn't then denied by authorize()'s "no
# policy registered" default-deny. Every staff role that can reach these two
# endpoints (routers/directory.py's `_ANY_STAFF_ROLE`) gets a read policy;
# `system_admin` needs no separate entry: `_admin_bypass` above already
# covers it.
@policy("doctor", "directory", "read")
def _doctor_reads_directory(user: User, resource: Any) -> bool:
    return True


@policy("nurse", "directory", "read")
def _nurse_reads_directory(user: User, resource: Any) -> bool:
    return True


@policy("front_desk", "directory", "read")
def _front_desk_reads_directory(user: User, resource: Any) -> bool:
    return True


@policy("lab_tech", "directory", "read")
def _lab_tech_reads_directory(user: User, resource: Any) -> bool:
    return True


@policy("pharmacist", "directory", "read")
def _pharmacist_reads_directory(user: User, resource: Any) -> bool:
    return True


@policy("billing_admin", "directory", "read")
def _billing_admin_reads_directory(user: User, resource: Any) -> bool:
    return True


# Frontend UUID-to-dropdown conversion follow-up (backend phase): `appointment`
# is the resource_type used for `GET /api/v1/appointments` (routers/
# appointments.py) -- a branch-wide "list appointments to check in" read, with
# no single ORM row to authorize against (a `appointment_service._BranchScoped`
# stand-in carries just `branch_id`, same pattern as `queue_token`'s own
# `list_queue`). Role gate matches `GET /queue/board`'s existing read-roles
# (front_desk/nurse/doctor/system_admin) since this feeds the queue check-in
# dropdown. As with every prior branch-scoped module's own policies above,
# the tenant guard inside `authorize()` is what actually enforces branch
# isolation here; these policies exist only so a role that passes the tenant
# guard isn't then denied by authorize()'s "no policy registered"
# default-deny. `system_admin` needs no separate entry: `_admin_bypass` above
# already covers it.
@policy("front_desk", "appointment", "read")
def _front_desk_reads_branch_appointments(user: User, resource: Any) -> bool:
    return True


@policy("nurse", "appointment", "read")
def _nurse_reads_branch_appointments(user: User, resource: Any) -> bool:
    return True


@policy("doctor", "appointment", "read")
def _doctor_reads_branch_appointments(user: User, resource: Any) -> bool:
    return True


# Whole-system review finding C1: DELETE /api/v1/appointments/{id} called
# `require_role(...)` only (role membership) with no `authorize()` call at
# all, so any front_desk/doctor/system_admin could cancel any appointment at
# any branch -- the exact cross-tenant write every other branch-scoped
# module's "authorize() before mutate" convention exists to prevent. These
# "write" policies plus the `authorize()` call added in
# routers/appointments.py close that gap; the tenant guard inside
# `authorize()` (branch_id match) is what actually enforces isolation here,
# same as the sibling "read" policies just above. `system_admin` needs no
# separate entry: `_admin_bypass` already covers it.
@policy("front_desk", "appointment", "write")
def _front_desk_writes_branch_appointments(user: User, resource: Any) -> bool:
    return True


@policy("doctor", "appointment", "write")
def _doctor_writes_branch_appointments(user: User, resource: Any) -> bool:
    return True
