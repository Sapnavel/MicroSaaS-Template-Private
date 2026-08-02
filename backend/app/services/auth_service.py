"""Auth business logic. Routers (routers/auth.py, routers/admin.py) stay
thin and delegate here — same "business logic lives in services/, router is
thin" split as services/scheduling_engine.py.

Design notes (see PRPs/auth-rbac-abac-prp.md, Security Design):
- `authenticate_user` returns one generic 401 message regardless of whether
  the email didn't exist or the password was wrong (OWASP: don't leak which
  half of the credential pair was invalid).
- Refresh tokens are never stored raw in `refresh_tokens.token_hash` — only
  a SHA-256 digest. SHA-256 (not bcrypt) is deliberate here: this is a
  lookup key for an already-high-entropy, server-generated 256-bit-ish JWT
  string, not a low-entropy user password, so a slow KDF buys nothing and
  only adds latency to every /auth/refresh call. Flagged as a judgment call
  in the phase report since the PRP didn't pin an exact algorithm.
- `/auth/refresh` rotates on use: the presented refresh token is revoked in
  the same transaction that issues the replacement pair, bounding a stolen
  refresh token to a single use.
"""

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import redis
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.tenant import Branch
from app.models.token import RefreshToken
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

# Same Redis client construction pattern as core/security.py / core/locking.py.
_redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)

# Precomputed bcrypt hash of a fixed dummy value, used to pay the same
# verify_password() cost for a nonexistent user as for a wrong password.
# Without this, `user is None` short-circuits `authenticate_user`'s `or`
# before bcrypt ever runs, making "no such account" measurably faster than
# "wrong password" — a timing oracle for account enumeration despite both
# cases returning the same generic 401 message (REVIEW-AGENT finding H1).
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-constant-time-auth-failure")


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _role_value(role: UserRole | str) -> str:
    return role.value if isinstance(role, UserRole) else role


def resolve_hospital_group_id(db: Session, branch_id: uuid.UUID | None) -> str | None:
    """Join through Branch to get hospital_group_id — User has no such
    column directly (see models/user.py vs models/tenant.py)."""
    if branch_id is None:
        return None
    branch = db.get(Branch, branch_id)
    return str(branch.hospital_group_id) if branch is not None else None


def authenticate_user(db: Session, email: str, password: str) -> User:
    """Shared by /auth/login and /auth/patient/login. Raises 401 with a
    single generic message on any failure — never reveals whether the
    account exists or the password was wrong.

    `verify_password` always runs, even when no such user exists (against
    `_DUMMY_PASSWORD_HASH` in that case), so a nonexistent email doesn't
    return measurably faster than a wrong password — see `_DUMMY_PASSWORD_HASH`.
    """
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    hash_to_check = user.hashed_password if user is not None and user.hashed_password else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, hash_to_check)
    if user is None or user.hashed_password is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    return user


def register_patient(db: Session, email: str, password: str, full_name: str) -> User:
    """Self-registration. Role is hardcoded to patient and branch_id to
    None here — never derived from client input (enforced upstream too by
    RegisterRequest's `extra="forbid"`)."""
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=UserRole.patient,
        branch_id=None,
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def provision_staff(
    db: Session,
    email: str,
    password: str,
    full_name: str,
    role: str,
    branch_id: uuid.UUID,
) -> User:
    """Admin-provisioned staff account. The only path that creates
    non-patient users. is_verified=True / is_active=True on create — an
    admin creating the account is itself the verification step."""
    try:
        role_enum = UserRole(role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid role") from exc

    if role_enum == UserRole.patient:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "patients self-register; cannot be admin-provisioned",
        )

    branch = db.get(Branch, branch_id)
    if branch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "branch not found")

    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role_enum,
        branch_id=branch_id,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def issue_token_pair(db: Session, user: User) -> tuple[str, str]:
    """Mint a fresh access+refresh pair for `user` and persist the refresh
    token's hash+expiry in `refresh_tokens`."""
    branch_id = str(user.branch_id) if user.branch_id else None
    hospital_group_id = resolve_hospital_group_id(db, user.branch_id)
    role = _role_value(user.role)
    subject = str(user.id)

    access_token = create_access_token(
        subject, role=role, branch_id=branch_id, hospital_group_id=hospital_group_id
    )
    refresh_token = create_refresh_token(
        subject, role=role, branch_id=branch_id, hospital_group_id=hospital_group_id
    )

    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_refresh_token(refresh_token),
            expires_at=expires_at,
            revoked=False,
        )
    )
    db.commit()
    return access_token, refresh_token


def rotate_refresh_token(db: Session, refresh_token_str: str) -> tuple[str, str]:
    """Rotate-on-use: validate the presented refresh token against the DB
    record (not just its signature — a valid signature on a revoked/expired
    row must still be rejected), revoke that row, and issue a brand new
    pair."""
    payload = decode_token(refresh_token_str)
    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")

    token_hash = _hash_refresh_token(refresh_token_str)
    row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).scalar_one_or_none()
    if row is None or row.revoked:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token invalid or revoked")

    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token expired")

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or inactive")

    row.revoked = True
    db.add(row)
    db.commit()

    return issue_token_pair(db, user)


def revoke_session(db: Session, access_token_payload: dict, refresh_token_str: str) -> None:
    """Logout: blocklist the access token's jti in Redis for the remainder
    of its own lifetime, and revoke the presented refresh token in the DB.
    `access_token_payload` is the already-decoded claims dict from
    `get_current_user` (stashed as `user.token_claims`) — no need to decode
    the access token again here."""
    jti = access_token_payload.get("jti")
    exp = access_token_payload.get("exp")
    if jti and exp is not None:
        ttl_seconds = int(exp - datetime.now(timezone.utc).timestamp())
        if ttl_seconds > 0:
            _redis_client.set(f"blocklist:{jti}", "1", ex=ttl_seconds)

    token_hash = _hash_refresh_token(refresh_token_str)
    row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).scalar_one_or_none()
    if row is not None and not row.revoked:
        row.revoked = True
        db.add(row)
        db.commit()
