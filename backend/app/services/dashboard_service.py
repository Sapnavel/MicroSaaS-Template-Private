"""Executive & Operational Dashboard module business logic
(PRPs/executive-dashboard-prp.md, Phase 1). Pure read-aggregation over data
that already exists -- no new tables, no new ORM models. Routers stay thin
and delegate here, same "business logic lives in services/, router is thin"
split as every other module.

INTERNAL RESULT TYPES: `schemas/dashboard.py` (Phase 2, a separate agent) has
not been written yet, so the five functions below return small
`@dataclass(frozen=True)` result types defined in this module
(`WardOccupancyResult`, `DepartmentWaitTimeResult`, `DailyRevenueResult`,
`NoShowRateResult`) rather than the Pydantic `*Response` schemas named in the
PRP. This mirrors `services/pharmacy_service.py`'s own `_BranchScoped`
dataclass-as-internal-contract pattern. Dataclasses were chosen over plain
dicts specifically so Phase 2 gets a typed, importable contract (field names
+ types) to build the Pydantic response schemas against, without either
phase having to guess the other's shape. Phase 2's router is expected to
call `ResponseModel.model_validate(result, from_attributes=True)` (or
construct the Pydantic model directly from each dataclass's fields) and then
delete this comment once `schemas/dashboard.py` exists -- these dataclasses
are a scaffold, not the permanent contract.

`get_stock_alerts` is the one exception: per the PRP's design decision #6 it
is a thin call-through to `pharmacy_service.get_low_stock`/`get_expiring`,
which already return real Pydantic schemas (`LowStockItemResponse`/
`ExpiringBatchResponse` from `schemas/pharmacy.py`). Its own return value is
therefore also a small dataclass (`StockAlertsResult`) simply bundling those
two already-typed lists -- no re-typing of pharmacy's own response shapes.

ACCESS CONTROL: per design decision #1/#5, every function in this module is
reachable by `system_admin` only (enforced by `require_role("system_admin")`
at the router, Phase 2) -- there is no non-admin caller, so `branch_id: None`
always means "aggregate across every branch," never "caller's own branch."
`_resolve_branch_filter`-style helpers from other services are deliberately
NOT reused here (they exist to resolve a non-admin caller's own branch, a
case that cannot occur in this module). No `authorize()` call is made for
the same reason (design decision #5) -- `current_user` is accepted on every
function purely for signature consistency with every other service module,
not because it's used.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import Invoice, InvoiceStatus
from app.models.queue import QueueToken
from app.models.resource import Specialty
from app.models.user import User
from app.models.ward import Bed, BedStatus, Ward
from app.schemas.pharmacy import ExpiringBatchResponse, LowStockItemResponse
from app.services import pharmacy_service


# --- Internal result types (scaffold for Phase 2's schemas/dashboard.py) ----


@dataclass(frozen=True)
class WardOccupancyResult:
    """One row per ward. `occupancy_pct` is `occupied_beds / total_beds` as a
    plain float in [0.0, 1.0] (Phase 2's Pydantic schema decides whether to
    render this as a fraction or multiply by 100 -- this layer just reports
    the ratio). `occupancy_pct` is `0.0` for a ward with zero beds rather
    than raising `ZeroDivisionError`."""

    branch_id: uuid.UUID
    ward_id: uuid.UUID
    ward_name: str
    ward_type: str
    total_beds: int
    occupied_beds: int
    occupancy_pct: float


@dataclass(frozen=True)
class DepartmentWaitTimeResult:
    """One row per (branch, department). Per design decision #2, this will
    be `[]` in this deployment today -- `queue_tokens` has zero rows because
    the Queue module's check-in endpoints (`routers/queue.py`) were never
    built. That is expected, correct behavior, not a bug: the query is
    written correctly against `checked_in_at`/`called_at` and starts
    returning real numbers the moment that module exists."""

    branch_id: uuid.UUID
    department_id: int | None
    department_name: str | None
    avg_wait_minutes: float
    sample_size: int


@dataclass(frozen=True)
class DailyRevenueResult:
    """One row per (branch, day). Bucketed by `Invoice.created_at` (invoice
    OPEN date), not "date paid" -- see design decision #4.
    `collected` = SUM(total_amount) for invoices opened that day whose
    CURRENT status happens to be 'paid'. This is an approximation, not a
    true "cash received on this date" figure: `Invoice` has no `paid_at`
    column, only a `created_at` (set once) and an in-place `status`, so an
    invoice opened on day X but paid weeks later is indistinguishable here
    from one paid the same day. `outstanding = gross_billed - collected`."""

    branch_id: uuid.UUID
    day: datetime
    invoice_count: int
    gross_billed: float
    collected: float
    outstanding: float


@dataclass(frozen=True)
class NoShowRateResult:
    """One row per branch (grouped-by-branch shape, for symmetry with
    `get_occupancy`/`get_revenue` -- see PRP SERVICE DESIGN section).
    `cancelled` AND `preempted` appointments are excluded from BOTH
    numerator and denominator entirely (neither is a no-show nor a
    completed visit -- see `get_no_show_rate`'s own docstring for the full
    `emergency_engine.py`/EXCLUDE-constraint rationale); every other status
    counts toward `total_considered`, only `no_show` counts toward
    `no_show_count`. `no_show_rate` is `0.0` when
    `total_considered == 0` rather than raising `ZeroDivisionError`.

    `no_show_risk_score_note`: per design decision #3, `Appointment.
    no_show_risk_score` is written nowhere in this codebase (no scoring
    service/heuristic/model exists to populate it) -- this endpoint reports
    only the ACTUAL no-show rate, it does not and cannot compare against
    that column's (always-NULL) predictions. This field surfaces that
    reality plainly rather than silently omitting any mention of it."""

    branch_id: uuid.UUID
    total_considered: int
    no_show_count: int
    no_show_rate: float
    no_show_risk_score_note: str = (
        "no_show_risk_score is NULL for every appointment in this deployment "
        "(no scoring service/heuristic/model has ever been built to populate "
        "it) -- this rate reflects only actual outcomes, not a comparison "
        "against predictions."
    )


@dataclass(frozen=True)
class StockAlertsResult:
    """Thin bundle of `pharmacy_service.get_low_stock`/`get_expiring`'s own,
    already-typed results -- see design decision #6. No re-implementation of
    either query."""

    low_stock: list[LowStockItemResponse]
    expiring_soon: list[ExpiringBatchResponse]


# --- Service functions -------------------------------------------------------


def get_occupancy(
    db: Session, current_user: User, branch_id: uuid.UUID | None
) -> list[WardOccupancyResult]:
    """GET /api/v1/dashboard/occupancy. One query joining `Ward` -> `Bed`,
    grouped by `(ward.branch_id, ward.id, ward.name, ward.ward_type)`.
    `branch_id=None` means "every branch" (system_admin-only module, see
    module docstring) -- filters by `Ward.branch_id` only when supplied."""
    occupied_count = func.count(Bed.id).filter(Bed.status == BedStatus.occupied)
    total_count = func.count(Bed.id)

    query = (
        select(
            Ward.branch_id,
            Ward.id,
            Ward.name,
            Ward.ward_type,
            total_count,
            occupied_count,
        )
        .join(Bed, Bed.ward_id == Ward.id, isouter=True)
        .group_by(Ward.branch_id, Ward.id, Ward.name, Ward.ward_type)
    )
    if branch_id is not None:
        query = query.where(Ward.branch_id == branch_id)

    rows = db.execute(query).all()
    results: list[WardOccupancyResult] = []
    for row_branch_id, ward_id, ward_name, ward_type, total_beds, occupied_beds in rows:
        total_beds = total_beds or 0
        occupied_beds = occupied_beds or 0
        occupancy_pct = (occupied_beds / total_beds) if total_beds > 0 else 0.0
        results.append(
            WardOccupancyResult(
                branch_id=row_branch_id,
                ward_id=ward_id,
                ward_name=ward_name,
                ward_type=ward_type,
                total_beds=total_beds,
                occupied_beds=occupied_beds,
                occupancy_pct=occupancy_pct,
            )
        )
    return results


def get_wait_times(
    db: Session, current_user: User, branch_id: uuid.UUID | None, days: int
) -> list[DepartmentWaitTimeResult]:
    """GET /api/v1/dashboard/wait-times?days=N (default 7 at the router).
    Only `QueueToken` rows with `called_at IS NOT NULL` count (a
    still-waiting/skipped token has no completed wait to measure).
    Grouped by `(branch_id, department_id)`, joined to `Specialty` for
    `department_name` (one joined query, not a per-row lookup -- same
    N+1-avoidance discipline `pharmacy_service.get_low_stock` uses).

    Per design decision #2, this returns `[]` in this deployment today --
    `queue_tokens` has zero rows because `routers/queue.py`'s check-in
    endpoints were never built. That is expected, not a bug."""
    window_start = datetime.now(timezone.utc) - timedelta(days=days)
    avg_wait_minutes = func.avg(
        func.extract("epoch", QueueToken.called_at - QueueToken.checked_in_at) / 60.0
    )
    sample_size = func.count(QueueToken.id)

    query = (
        select(
            QueueToken.branch_id,
            QueueToken.department_id,
            Specialty.name,
            avg_wait_minutes,
            sample_size,
        )
        .outerjoin(Specialty, Specialty.id == QueueToken.department_id)
        .where(
            QueueToken.checked_in_at >= window_start,
            QueueToken.called_at.is_not(None),
        )
        .group_by(QueueToken.branch_id, QueueToken.department_id, Specialty.name)
    )
    if branch_id is not None:
        query = query.where(QueueToken.branch_id == branch_id)

    rows = db.execute(query).all()
    return [
        DepartmentWaitTimeResult(
            branch_id=row_branch_id,
            department_id=department_id,
            department_name=department_name,
            avg_wait_minutes=float(avg_minutes) if avg_minutes is not None else 0.0,
            sample_size=count,
        )
        for row_branch_id, department_id, department_name, avg_minutes, count in rows
    ]


def get_revenue(
    db: Session, current_user: User, branch_id: uuid.UUID | None, days: int
) -> list[DailyRevenueResult]:
    """GET /api/v1/dashboard/revenue?days=N (default 30 at the router).
    Query `Invoice` where `created_at >= now() - days` AND `status !=
    'void'`, grouped by `(branch_id, DATE(created_at))`. See
    `DailyRevenueResult`'s docstring and design decision #4 for why
    `collected` is an approximation (no `paid_at` column exists)."""
    window_start = datetime.now(timezone.utc) - timedelta(days=days)
    day_bucket = func.date(Invoice.created_at)
    invoice_count = func.count(Invoice.id)
    gross_billed = func.coalesce(func.sum(Invoice.total_amount), 0)
    collected = func.coalesce(
        func.sum(Invoice.total_amount).filter(Invoice.status == InvoiceStatus.paid.value),
        0,
    )

    query = (
        select(
            Invoice.branch_id,
            day_bucket,
            invoice_count,
            gross_billed,
            collected,
        )
        .where(
            Invoice.created_at >= window_start,
            Invoice.status != InvoiceStatus.void.value,
        )
        .group_by(Invoice.branch_id, day_bucket)
        .order_by(Invoice.branch_id, day_bucket)
    )
    if branch_id is not None:
        query = query.where(Invoice.branch_id == branch_id)

    rows = db.execute(query).all()
    results: list[DailyRevenueResult] = []
    for row_branch_id, day, count, gross, collected_amount in rows:
        gross_f = float(gross or 0)
        collected_f = float(collected_amount or 0)
        results.append(
            DailyRevenueResult(
                branch_id=row_branch_id,
                day=day,
                invoice_count=count,
                gross_billed=gross_f,
                collected=collected_f,
                outstanding=gross_f - collected_f,
            )
        )
    return results


def get_no_show_rate(
    db: Session, current_user: User, branch_id: uuid.UUID | None, days: int
) -> list[NoShowRateResult]:
    """GET /api/v1/dashboard/no-shows?days=N (default 30 at the router).
    Query `Appointment` where the lower bound of `time_range` falls in
    `[now() - days, now()]`, using `func.lower(Appointment.time_range)` --
    the standard Postgres range lower-bound extraction. This is the first
    place in the codebase filtering appointments by a plain reporting
    time-window (rather than a conflict-check range operator); no existing
    helper does this (`scheduling_engine.py` only uses `&&`/`>>` for
    booking-conflict checks, a different concern -- confirmed by reading
    that module), so this filter is written fresh here rather than reusing
    or inventing a false precedent.

    `cancelled` AND `preempted` appointments are excluded entirely (neither
    numerator nor denominator) -- `preempted` is grouped with `cancelled` in
    the exact same "no longer an active booking" category
    `models/appointment.py`'s own EXCLUDE constraint and
    `emergency_engine.py` (line ~86) already use
    (`NOT IN ('cancelled', 'no_show', 'preempted')`); a preempted
    appointment was bumped by the system for a higher-priority emergency
    case, not a patient no-show, and counting it in the denominator would
    understate the true no-show rate by diluting it with an outcome that was
    never the patient's fault. Every other status counts toward the
    denominator (`total_considered`); only `no_show` counts toward the
    numerator (`no_show_count`). Returns one row per branch (grouped-by-branch,
    consistent with `get_occupancy`/`get_revenue`'s list-of-groups shape),
    even when `branch_id` is supplied (in which case the list has at most
    one element)."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    lower_bound = func.lower(Appointment.time_range)

    total_considered = func.count(Appointment.id)
    no_show_count = func.count(Appointment.id).filter(
        Appointment.status == AppointmentStatus.no_show
    )

    query = (
        select(Appointment.branch_id, total_considered, no_show_count)
        .where(
            lower_bound >= window_start,
            lower_bound <= now,
            Appointment.status.not_in([AppointmentStatus.cancelled, AppointmentStatus.preempted]),
        )
        .group_by(Appointment.branch_id)
    )
    if branch_id is not None:
        query = query.where(Appointment.branch_id == branch_id)

    rows = db.execute(query).all()
    results: list[NoShowRateResult] = []
    for row_branch_id, considered, no_shows in rows:
        rate = (no_shows / considered) if considered > 0 else 0.0
        results.append(
            NoShowRateResult(
                branch_id=row_branch_id,
                total_considered=considered,
                no_show_count=no_shows,
                no_show_rate=rate,
            )
        )
    return results


def get_stock_alerts(
    db: Session, current_user: User, branch_id: uuid.UUID | None
) -> StockAlertsResult:
    """GET /api/v1/dashboard/stock-alerts. Per design decision #6, a thin
    call-through to `pharmacy_service.get_low_stock`/`get_expiring` -- does
    NOT reimplement either query. Those functions' own `_resolve_branch_
    filter`/`authorize` calls are no-ops for a `system_admin` caller
    (`_resolve_branch_filter` returns `branch_id` unchanged for that role),
    so this call-through is safe and adds no redundant restriction."""
    low_stock = pharmacy_service.get_low_stock(db, current_user, branch_id)
    expiring_soon = pharmacy_service.get_expiring(db, current_user, branch_id, within_days=30)
    return StockAlertsResult(low_stock=low_stock, expiring_soon=expiring_soon)
