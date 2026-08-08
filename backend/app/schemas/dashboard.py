"""Pydantic response schemas for the Executive & Operational Dashboard module
(PRPs/executive-dashboard-prp.md, Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT, working
in parallel against the same PRP -- do not rename fields without updating
the frontend in lockstep.

These schemas mirror `services/dashboard_service.py`'s internal
`@dataclass(frozen=True)` result types (`WardOccupancyResult`,
`DepartmentWaitTimeResult`, `DailyRevenueResult`, `NoShowRateResult`,
`StockAlertsResult`) field-for-field -- `model_config =
ConfigDict(from_attributes=True)` lets the router build each response
directly via `ResponseModel.model_validate(result, from_attributes=True)`
since dataclasses support attribute access.

There are no request body schemas in this module: every endpoint is a
read-only `GET` with query params only (`branch_id`, `days`) -- see the
PRP's ENDPOINTS table.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.pharmacy import ExpiringBatchResponse, LowStockItemResponse


class WardOccupancyResponse(BaseModel):
    """GET /api/v1/dashboard/occupancy response line. One row per ward.
    `occupancy_pct` is `occupied_beds / total_beds` as a plain float in
    [0.0, 1.0] (`0.0` for a ward with zero beds)."""

    model_config = ConfigDict(from_attributes=True)

    branch_id: uuid.UUID
    ward_id: uuid.UUID
    ward_name: str
    ward_type: str
    total_beds: int
    occupied_beds: int
    occupancy_pct: float


class DepartmentWaitTimeResponse(BaseModel):
    """GET /api/v1/dashboard/wait-times response line. One row per
    (branch, department). Per the PRP's design decision #2, this returns
    `[]` in this deployment today -- `queue_tokens` has zero rows because
    the Queue module's check-in endpoints (`routers/queue.py`) were never
    built. That is expected, correct behavior, not a bug: the underlying
    query is written correctly and will start returning real numbers the
    moment a future PRP builds that module's check-in endpoint."""

    model_config = ConfigDict(from_attributes=True)

    branch_id: uuid.UUID
    department_id: int | None
    department_name: str | None
    avg_wait_minutes: float
    sample_size: int


class DailyRevenueResponse(BaseModel):
    """GET /api/v1/dashboard/revenue response line. One row per
    (branch, day), bucketed by `Invoice.created_at` (invoice OPEN date),
    not "date paid" -- see the PRP's design decision #4. `Invoice` has no
    `paid_at`/`updated_at` column, only a `created_at` (set once) and an
    in-place `status`, so `collected` (SUM(total_amount) for invoices
    opened that day whose CURRENT status happens to be 'paid') is an
    approximation, not a true "cash received on this date" figure. An
    invoice opened on day X but paid weeks later is indistinguishable here
    from one paid the same day. `outstanding = gross_billed - collected`."""

    model_config = ConfigDict(from_attributes=True)

    branch_id: uuid.UUID
    day: datetime
    invoice_count: int
    gross_billed: float
    collected: float
    outstanding: float


class NoShowRateResponse(BaseModel):
    """GET /api/v1/dashboard/no-shows response line. One row per branch
    (grouped-by-branch shape, for symmetry with `WardOccupancyResponse`/
    `DailyRevenueResponse`'s list-of-groups shape), even when `branch_id`
    is supplied (in which case the list has at most one element).
    `cancelled` and `preempted` appointments are excluded from both
    numerator and denominator entirely -- neither is a no-show nor a
    completed visit. `no_show_rate` is `0.0` when `total_considered == 0`.

    `avg_predicted_no_show_risk`: mean `Appointment.no_show_risk_score`
    across the same `total_considered` rows -- HMS Project Completion Prompt
    gap fix: `scheduling_engine.score_no_show_risk` now runs and persists a
    score on every booking (`book_appointment`), where it previously had
    zero callers and this column was NULL for every row. `None` when no row
    in the window has a non-NULL score yet (a fresh deployment with only
    pre-existing, unscored rows in this window).

    `no_show_risk_score_note`: kept for continuity even now that scoring is
    wired up -- explains what the average IS (a predicted-at-booking-time
    heuristic) versus what `no_show_rate` IS (actual outcomes), so the two
    numbers are never misread as the same thing."""

    model_config = ConfigDict(from_attributes=True)

    branch_id: uuid.UUID
    total_considered: int
    no_show_count: int
    no_show_rate: float
    avg_predicted_no_show_risk: float | None
    no_show_risk_score_note: str


class StockAlertsResponse(BaseModel):
    """GET /api/v1/dashboard/stock-alerts response. Per the PRP's design
    decision #6, a thin bundle of `pharmacy_service.get_low_stock`/
    `get_expiring`'s own, already-typed results -- `LowStockItemResponse`/
    `ExpiringBatchResponse` are imported from `schemas/pharmacy.py`, not
    redefined here."""

    model_config = ConfigDict(from_attributes=True)

    low_stock: list[LowStockItemResponse]
    expiring_soon: list[ExpiringBatchResponse]
