"""Ward, Bed & OT module business logic (PRPs/ward-bed-ot-module-prp.md,
Phase 2). Routers (routers/wards.py) stay thin and delegate here -- same
"business logic lives in services/, router is thin" split as
services/consultation_service.py, services/patient_service.py,
services/pharmacy_service.py.

Unlike Pharmacy's `dispense` (engine mutates first, `authorize()` is called
only after the engine call returns -- REVIEW-AGENT flagged that ordering as
fragile in PRPs/pharmacy-module-prp.md Phase 3, since a token/branch
reassignment race would let the engine's mutation happen before the tenant
guard ever runs), every mutating function below resolves the affected
Ward/Room and calls `core.security.authorize` BEFORE invoking any
`ward_engine` function. `ward_engine`'s functions commit internally (see
that module's docstring), so there is no "authorize after the fact, but
before commit" middle ground here the way Pharmacy's transaction shape
allowed -- authorize-first is the only ordering that keeps a denied caller
from ever causing a state mutation.

BRANCH RESOLUTION JUDGMENT CALLS (flagged per the Phase 2 brief):

1. Admission/discharge/transfer/bed-status endpoints resolve the acted-on
   resource's branch by walking `Bed -> Ward -> branch_id` (transfer walks
   BOTH the old and new bed's ward) and pass the actual `Ward` ORM object to
   `authorize(current_user, "ward", "write"/"read", ward)`. `Ward` already
   has a real `branch_id` column, so no `_BranchScoped` stand-in is needed
   for these -- unlike Pharmacy, which needed one because `InventoryItem`
   sometimes isn't loaded (the two GET aggregate endpoints). This module's
   `authorize()` calls therefore all resolve to the SAME tenant guard in
   `core/security.py` (resource has `.branch_id`, callers who are not
   `system_admin` must match it) -- `authorize()` denies-by-default via that
   guard, and the `nurse`/`doctor`/`front_desk` `"ward"` policies registered
   in `core/security.py` exist only so a role that passes the tenant guard
   isn't then denied by "no policy registered" (same reminder every prior
   module's PRP includes).

2. OT scheduling reuses the SAME `"ward"` resource_type (not a second one)
   for its `authorize()` calls, passing the `Room` object directly --
   `Room.branch_id` is a real column, so this works the same way. An OT room
   is still a ward-module concern per the Phase 2 brief's own wording ("your
   call, document it") -- introducing a second resource_type here would only
   duplicate the exact same nurse/doctor/front_desk policy registrations for
   no behavioral difference, since the tenant guard (not the policy body)
   is what actually enforces branch isolation in every case below.

3. `list_beds`/`list_ot_schedules` (the two GET list endpoints): a
   `system_admin` caller may omit the branch filter entirely ("all
   branches"), matching `pharmacy_service.get_low_stock`/`get_expiring`'s
   established convention. Every other role MUST resolve to a concrete own-
   branch value -- for `list_beds` this is the `branch_id` query param
   (required, and rejected with 422 if it doesn't match the caller's own
   branch, exactly `pharmacy_service._resolve_branch_filter`'s mismatch
   rule); for `list_ot_schedules` there is no `branch_id` query param in the
   contract at all (only `room_id`/`surgeon_id`/`from_time`/`to_time`), so
   the caller's own branch is applied as a silent `Room.branch_id` join
   filter instead of a client-suppliable parameter -- a non-admin caller
   simply cannot see another branch's OT schedule, there is nothing to
   mismatch-reject here since there is no field carrying a conflicting
   claim.

4. Both list endpoints additionally call `authorize(..., "read", ...)` with
   a `_BranchScoped` stand-in once a concrete branch_id is known (mirroring
   Pharmacy's use of that dataclass for the same "no single ORM row" reason)
   -- defense-in-depth alongside the query-level filtering, not the only
   thing preventing cross-branch reads.

RANGE-BOUND EXTRACTION: `AdmissionResponse`/`OTScheduleResponse` need plain
`start_time`/`end_time` fields, but `Admission.stay_range`/
`OTSchedule.time_range` come back from the ORM as psycopg2 `Range` objects
(see `ward_engine._lower_bound_isoformat`'s docstring for the same
observation). `_range_bound_as_datetime` below reads `.lower`/`.upper` off
the already-loaded range and returns a `datetime` (parsing it if the driver
ever hands back a string instead, mirroring `_lower_bound_isoformat`'s own
`isinstance(..., str)` guard) -- `stay_range.upper` is `None` for an
ongoing/undischarged admission and is passed through as `None` rather than
assumed to always be set.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import authorize, get_caller_branch_id
from app.models.patient import Patient
from app.models.resource import Doctor, Room
from app.models.user import User, UserRole
from app.models.ward import Admission, Bed, BedStatus, OTSchedule, Ward
from app.schemas.ward import (
    AdmissionCreate,
    AdmissionResponse,
    BedMatrixEntryResponse,
    BedResponse,
    BedStatusUpdateRequest,
    DischargeRequest,
    OTScheduleCreate,
    OTScheduleResponse,
    TransferRequest,
)
from app.services import ward_engine


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard when there
    is no single ORM row to check against -- the two GET list endpoints, when
    scoped to one branch. Same pattern as `pharmacy_service._BranchScoped`."""

    branch_id: uuid.UUID


def _range_bound_as_datetime(value: object) -> datetime | None:
    """Extract a `TSTZRANGE` bound (already-loaded psycopg2 `Range.lower` /
    `.upper`) as a `datetime`, or `None` if the bound is unset (an
    open-ended admission's `.upper`). See module docstring's "RANGE-BOUND
    EXTRACTION" section."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"unexpected range bound type: {type(value)!r}")


def _shape_admission(admission: Admission) -> AdmissionResponse:
    return AdmissionResponse(
        id=admission.id,
        patient_id=admission.patient_id,
        bed_id=admission.bed_id,
        admitted_by=admission.admitted_by,
        start_time=_range_bound_as_datetime(admission.stay_range.lower),
        end_time=_range_bound_as_datetime(admission.stay_range.upper),
        discharged_at=admission.discharged_at,
    )


def _shape_ot_schedule(ot_schedule: OTSchedule) -> OTScheduleResponse:
    start_time = _range_bound_as_datetime(ot_schedule.time_range.lower)
    end_time = _range_bound_as_datetime(ot_schedule.time_range.upper)
    assert start_time is not None and end_time is not None  # OT slots are always closed ranges
    return OTScheduleResponse(
        id=ot_schedule.id,
        room_id=ot_schedule.room_id,
        patient_id=ot_schedule.patient_id,
        surgeon_id=ot_schedule.surgeon_id,
        start_time=start_time,
        end_time=end_time,
    )


def _get_bed_or_404(db: Session, bed_id: uuid.UUID) -> Bed:
    bed = db.get(Bed, bed_id)
    if bed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bed not found")
    return bed


def _get_ward_for_bed(db: Session, bed: Bed) -> Ward:
    ward = db.get(Ward, bed.ward_id)
    assert ward is not None  # FK guarantees this; bed couldn't exist otherwise
    return ward


def _get_patient_or_404(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found")
    return patient


def _get_admission_or_404(db: Session, admission_id: uuid.UUID) -> Admission:
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admission not found")
    return admission


# --- Admissions --------------------------------------------------------------


def admit(db: Session, current_user: User, payload: AdmissionCreate) -> AdmissionResponse:
    """POST /api/v1/wards/admissions. Validates bed/patient existence and
    authorizes against the bed's ward BEFORE calling `ward_engine.admit_patient`
    (see module docstring) -- `BedNotAvailableError`/`BedConflictError`/
    `ResourceBusyError` from the engine itself propagate uncaught to the
    router, which maps them to 409/409/423 respectively."""
    bed = _get_bed_or_404(db, payload.bed_id)
    _get_patient_or_404(db, payload.patient_id)
    ward = _get_ward_for_bed(db, bed)

    authorize(current_user, "ward", "write", ward)

    admission = ward_engine.admit_patient(
        db,
        patient_id=payload.patient_id,
        bed_id=payload.bed_id,
        start_time=payload.start_time,
        admitted_by=current_user.id,
    )
    return _shape_admission(admission)


def discharge(
    db: Session, current_user: User, admission_id: uuid.UUID, payload: DischargeRequest
) -> AdmissionResponse:
    """PATCH /api/v1/wards/admissions/{id}/discharge. Resolves the admission's
    bed's ward and authorizes BEFORE calling `ward_engine.discharge_patient`
    -- `AlreadyDischargedError`/`ResourceBusyError` from the engine propagate
    uncaught to the router (409/423)."""
    admission = _get_admission_or_404(db, admission_id)
    bed = _get_bed_or_404(db, admission.bed_id)
    ward = _get_ward_for_bed(db, bed)

    authorize(current_user, "ward", "write", ward)

    updated = ward_engine.discharge_patient(
        db,
        admission_id=admission_id,
        discharged_by=current_user.id,
        discharge_time=payload.discharge_time,
    )
    return _shape_admission(updated)


def transfer(
    db: Session, current_user: User, admission_id: uuid.UUID, payload: TransferRequest
) -> AdmissionResponse:
    """PATCH /api/v1/wards/admissions/{id}/transfer. Authorizes against BOTH
    the old and the new bed's ward before calling
    `ward_engine.transfer_patient` -- either mismatch is a 403, per the
    Phase 2 brief ("check both")."""
    admission = _get_admission_or_404(db, admission_id)
    old_bed = _get_bed_or_404(db, admission.bed_id)
    old_ward = _get_ward_for_bed(db, old_bed)
    new_bed = _get_bed_or_404(db, payload.new_bed_id)
    new_ward = _get_ward_for_bed(db, new_bed)

    authorize(current_user, "ward", "write", old_ward)
    authorize(current_user, "ward", "write", new_ward)

    new_admission = ward_engine.transfer_patient(
        db,
        admission_id=admission_id,
        new_bed_id=payload.new_bed_id,
        actor_user_id=current_user.id,
    )
    return _shape_admission(new_admission)


# --- Bed status ---------------------------------------------------------------


def update_bed_status(
    db: Session, current_user: User, bed_id: uuid.UUID, payload: BedStatusUpdateRequest
) -> BedResponse:
    """PATCH /api/v1/wards/beds/{id}/status. Authorizes against the bed's
    ward BEFORE calling `ward_engine.set_bed_status` --
    `IllegalBedStatusTransition`/`ResourceBusyError` from the engine
    propagate uncaught to the router (409/423)."""
    bed = _get_bed_or_404(db, bed_id)
    ward = _get_ward_for_bed(db, bed)

    authorize(current_user, "ward", "write", ward)

    updated_bed = ward_engine.set_bed_status(
        db,
        bed_id=bed_id,
        requested_status=BedStatus(payload.status),
        actor_user_id=current_user.id,
    )
    return BedResponse.model_validate(updated_bed)


# --- OT scheduling --------------------------------------------------------------


def schedule_ot(db: Session, current_user: User, payload: OTScheduleCreate) -> OTScheduleResponse:
    """POST /api/v1/wards/ot-schedule. Validates room/patient/surgeon
    existence BEFORE calling the engine (so a bad id gets a clean 404 rather
    than a raw FK `IntegrityError` from the engine's insert -- see the Phase
    2 brief), authorizes against the room BEFORE calling the engine (see
    module docstring point 2), then calls `ward_engine.schedule_ot`.
    `OTConflictError`/`ResourceBusyError` propagate uncaught to the router
    (409/423)."""
    room = db.get(Room, payload.room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "room not found")
    _get_patient_or_404(db, payload.patient_id)
    surgeon = db.get(Doctor, payload.surgeon_id)
    if surgeon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "surgeon not found")

    authorize(current_user, "ward", "write", room)

    ot_schedule = ward_engine.schedule_ot(
        db,
        room_id=payload.room_id,
        patient_id=payload.patient_id,
        surgeon_id=payload.surgeon_id,
        start_time=payload.start_time,
        duration_minutes=payload.duration_minutes,
        actor_user_id=current_user.id,
    )
    return _shape_ot_schedule(ot_schedule)


# --- List / branch-scoped GETs -----------------------------------------------


def _resolve_branch_filter(current_user: User, branch_id_param: uuid.UUID | None) -> uuid.UUID | None:
    """Shared branch-resolution helper for `list_beds`. `system_admin` may
    omit `branch_id` to mean "all branches"; every other role MUST supply
    `branch_id` and it MUST match their own (token-claims-preferred, via
    `get_caller_branch_id` -- never `current_user.branch_id` directly, see
    the Phase 2 brief's explicit warning about the Pharmacy module's
    original mistake). Mismatch or omission for a non-admin caller is 422,
    mirroring `pharmacy_service._resolve_branch_filter`'s "never silently
    ignore conflicting/missing client input" discipline."""
    if current_user.role == UserRole.system_admin:
        return branch_id_param

    caller_branch_id = get_caller_branch_id(current_user)
    if caller_branch_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "your account has no branch assigned; contact an administrator",
        )
    caller_branch_uuid = uuid.UUID(caller_branch_id)
    if branch_id_param is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "branch_id is required")
    if branch_id_param != caller_branch_uuid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "branch_id in the request does not match your assigned branch",
        )
    return caller_branch_uuid


def list_beds(
    db: Session,
    current_user: User,
    branch_id_param: uuid.UUID | None,
    ward_id_param: uuid.UUID | None,
    status_param: BedStatus | None,
) -> list[BedMatrixEntryResponse]:
    """GET /api/v1/wards/beds?branch_id=&ward_id=&status="""
    branch_id = _resolve_branch_filter(current_user, branch_id_param)
    if branch_id is not None:
        authorize(current_user, "ward", "read", _BranchScoped(branch_id=branch_id))
    # else: system_admin querying across every branch -- no single resource to
    # authorize against; system_admin already bypasses the tenant guard inside
    # authorize() (core/security.py's _admin_bypass), and no other role can
    # reach this branch (non-admin always resolves to a concrete branch_id
    # above).

    query = select(
        Bed.id,
        Bed.ward_id,
        Ward.name,
        Ward.branch_id,
        Bed.label,
        Bed.status,
    ).join(Ward, Ward.id == Bed.ward_id)

    if branch_id is not None:
        query = query.where(Ward.branch_id == branch_id)
    if ward_id_param is not None:
        query = query.where(Bed.ward_id == ward_id_param)
    if status_param is not None:
        query = query.where(Bed.status == status_param)

    rows = db.execute(query).all()
    return [
        BedMatrixEntryResponse(
            id=row[0],
            ward_id=row[1],
            ward_name=row[2],
            branch_id=row[3],
            label=row[4],
            status=row[5].value if isinstance(row[5], BedStatus) else row[5],
        )
        for row in rows
    ]


def list_ot_schedules(
    db: Session,
    current_user: User,
    room_id_param: uuid.UUID | None,
    surgeon_id_param: uuid.UUID | None,
    from_time: datetime | None,
    to_time: datetime | None,
) -> list[OTScheduleResponse]:
    """GET /api/v1/wards/ot-schedule?room_id=&surgeon_id=&from_time=&to_time=

    At least one of `room_id`/`surgeon_id`/(`from_time` AND `to_time`) is
    required -- 422 otherwise (same "no unbounded scan" rule every prior
    module's list endpoint uses). Branch-scoped via `Room.branch_id`: unlike
    `list_beds`, there is no `branch_id` query param in this contract at all
    -- a non-admin caller's own branch is applied as a silent join filter
    instead (see module docstring point 3)."""
    if room_id_param is None and surgeon_id_param is None and not (from_time and to_time):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "at least one of room_id, surgeon_id, or from_time+to_time is required",
        )

    query = select(
        OTSchedule.id,
        OTSchedule.room_id,
        OTSchedule.patient_id,
        OTSchedule.surgeon_id,
        OTSchedule.time_range,
        Room.branch_id,
    ).join(Room, Room.id == OTSchedule.room_id)

    if current_user.role == UserRole.system_admin:
        pass  # all branches
    else:
        caller_branch_id = get_caller_branch_id(current_user)
        if caller_branch_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "your account has no branch assigned; contact an administrator",
            )
        caller_branch_uuid = uuid.UUID(caller_branch_id)
        authorize(current_user, "ward", "read", _BranchScoped(branch_id=caller_branch_uuid))
        query = query.where(Room.branch_id == caller_branch_uuid)

    if room_id_param is not None:
        query = query.where(OTSchedule.room_id == room_id_param)
    if surgeon_id_param is not None:
        query = query.where(OTSchedule.surgeon_id == surgeon_id_param)
    if from_time is not None and to_time is not None:
        range_literal = f"[{from_time.isoformat()},{to_time.isoformat()})"
        query = query.where(OTSchedule.time_range.op("&&")(range_literal))

    rows = db.execute(query).all()
    results = []
    for row in rows:
        time_range = row[4]
        start_time = _range_bound_as_datetime(time_range.lower)
        end_time = _range_bound_as_datetime(time_range.upper)
        assert start_time is not None and end_time is not None
        results.append(
            OTScheduleResponse(
                id=row[0],
                room_id=row[1],
                patient_id=row[2],
                surgeon_id=row[3],
                start_time=start_time,
                end_time=end_time,
            )
        )
    return results
