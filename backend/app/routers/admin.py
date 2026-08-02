"""Admin-only endpoints. Currently just staff provisioning. Kept as its own
router (separate prefix from /auth) since it's a distinct concern —
provisioning accounts vs. authenticating them — per
PRPs/auth-rbac-abac-prp.md.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.auth import StaffProfile, StaffProvisionRequest
from app.services import auth_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/staff", response_model=StaffProfile, status_code=status.HTTP_201_CREATED)
def provision_staff(
    payload: StaffProvisionRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_role("system_admin")),
) -> StaffProfile:
    user = auth_service.provision_staff(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        role=payload.role,
        branch_id=payload.branch_id,
    )
    return StaffProfile.model_validate(user)
