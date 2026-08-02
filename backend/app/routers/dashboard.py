"""Executive & Operational Dashboard module (PRPs/executive-dashboard-prp.md,
Phase 2). Business logic lives in services/dashboard_service.py -- this
module is request validation and role gating only, matching the thin-router
pattern in routers/notifications.py / routers/billing.py.

Per the PRP's design decision #1/#5: access is `system_admin` only, for all
five endpoints -- there is no non-admin caller anywhere in this module, so
`require_role("system_admin")` is the entire access-control surface. No
`authorize()` call is added on top of it: every resource here is a plain
aggregate (no single ORM row with a `branch_id` attribute to check), and
`system_admin` already bypasses `authorize()`'s tenant guard unconditionally,
so a resource-scoped `authorize()` call would be checking a condition that
can never fail -- dead code. See the PRP's design decision #5 for the full
rationale.

`branch_id` is optional on every endpoint: omitted means "aggregate across
every branch" (system_admin has no branch of their own), supplied means
"scope to this one branch."
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_role
from app.database import get_db
from app.models.user import User
from app.schemas.dashboard import (
    DailyRevenueResponse,
    DepartmentWaitTimeResponse,
    NoShowRateResponse,
    StockAlertsResponse,
    WardOccupancyResponse,
)
from app.services import dashboard_service

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

_ROLES = ("system_admin",)


@router.get("/occupancy")
def get_occupancy(
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ROLES)),
) -> list[WardOccupancyResponse]:
    results = dashboard_service.get_occupancy(db, current_user, branch_id)
    return [WardOccupancyResponse.model_validate(r, from_attributes=True) for r in results]


@router.get("/wait-times")
def get_wait_times(
    branch_id: uuid.UUID | None = None,
    days: int = Query(default=7, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ROLES)),
) -> list[DepartmentWaitTimeResponse]:
    results = dashboard_service.get_wait_times(db, current_user, branch_id, days)
    return [DepartmentWaitTimeResponse.model_validate(r, from_attributes=True) for r in results]


@router.get("/revenue")
def get_revenue(
    branch_id: uuid.UUID | None = None,
    days: int = Query(default=30, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ROLES)),
) -> list[DailyRevenueResponse]:
    results = dashboard_service.get_revenue(db, current_user, branch_id, days)
    return [DailyRevenueResponse.model_validate(r, from_attributes=True) for r in results]


@router.get("/no-shows")
def get_no_show_rate(
    branch_id: uuid.UUID | None = None,
    days: int = Query(default=30, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ROLES)),
) -> list[NoShowRateResponse]:
    results = dashboard_service.get_no_show_rate(db, current_user, branch_id, days)
    return [NoShowRateResponse.model_validate(r, from_attributes=True) for r in results]


@router.get("/stock-alerts")
def get_stock_alerts(
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ROLES)),
) -> StockAlertsResponse:
    result = dashboard_service.get_stock_alerts(db, current_user, branch_id)
    return StockAlertsResponse.model_validate(result, from_attributes=True)
