"""Auth endpoints: registration, staff/patient login, refresh, logout, and
the current-user profile. Business logic lives in services/auth_service.py
— this module is request validation + response shaping only, matching the
pattern in routers/appointments.py.

Staff account provisioning (POST /admin/staff) lives in routers/admin.py,
not here — separate prefix, separate concern (see
PRPs/auth-rbac-abac-prp.md, FILES TO CREATE / MODIFY).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.rate_limit import check_login_ip_rate_limit, check_login_rate_limit
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User, UserRole
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
    UserProfile,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
    user = auth_service.register_patient(db, payload.email, payload.password, payload.full_name)
    return RegisterResponse.model_validate(user)


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    """Staff portal login: doctor, nurse, front_desk, lab_tech, pharmacist,
    billing_admin, system_admin. Patients are rejected here — they use
    /auth/patient/login."""
    check_login_rate_limit(f"login_attempts:{payload.email}")
    check_login_ip_rate_limit(request.client.host if request.client else "unknown")
    user = auth_service.authenticate_user(db, payload.email, payload.password)

    if user.role == UserRole.patient:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    if not user.is_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account not verified")
    if user.branch_id is None and user.role != UserRole.system_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account has no branch assigned")

    access_token, refresh_token = auth_service.issue_token_pair(db, user)
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post("/patient/login", response_model=TokenPair)
def patient_login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    """Patient portal login. Kept as a separate route from staff login (not
    just a shared function) per PRP Security Design section 5 — the most
    likely place to later add MFA/OTP without touching staff auth."""
    check_login_rate_limit(f"login_attempts:{payload.email}")
    check_login_ip_rate_limit(request.client.host if request.client else "unknown")
    user = auth_service.authenticate_user(db, payload.email, payload.password)

    if user.role != UserRole.patient:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")

    access_token, refresh_token = auth_service.issue_token_pair(db, user)
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenPair:
    access_token, refresh_token = auth_service.rotate_refresh_token(db, payload.refresh_token)
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: LogoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    # get_current_user always stashes the validated access-token claims on
    # the returned User instance (see core/security.py) — reuse them
    # instead of decoding the bearer token a second time.
    token_claims: dict = getattr(current_user, "token_claims", {})
    auth_service.revoke_session(db, token_claims, payload.refresh_token)


@router.get("/me", response_model=UserProfile)
def me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> UserProfile:
    hospital_group_id = auth_service.resolve_hospital_group_id(db, current_user.branch_id)
    return UserProfile(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role.value,
        branch_id=current_user.branch_id,
        hospital_group_id=hospital_group_id,
        is_active=current_user.is_active,
        is_verified=current_user.is_verified,
    )
