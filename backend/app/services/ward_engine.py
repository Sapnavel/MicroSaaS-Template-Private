"""Ward / Bed / OT booking engine (PRPs/ward-bed-ot-module-prp.md, "ENGINE
DESIGN"). Structural cousin of `services/scheduling_engine.py`: same
concurrency shape (acquire a Redis lock via `core.locking.lock_manager`,
then let a Postgres `EXCLUDE USING gist` constraint be the authoritative
backstop, catching `IntegrityError` and turning it into a clean 409-worthy
exception) applied to two new resources -- a physical bed and an OT room +
surgeon pair -- instead of a doctor/room/equipment triple.

Unlike the Lab module's `lab_workflow.py`/`lab_service.py` split ("engine
computes, service persists+audits"), the five functions below own their own
commit + audit log call, exactly as the PRP's "ENGINE DESIGN" section
describes each of them ending in "commit, audit-log": the Redis lock scope
IS the natural transaction boundary here (same as `book_appointment` above,
which also commits and publishes an event inside its own lock scope), so
introducing an extra persistence layer on top would be a needless
indirection this module has no use for.

Three judgment calls worth flagging up front (Phase 2 depends on the exact
shapes below):

(a) EXCEPTION NAMES -- mirroring `scheduling_engine.py`'s
    `BookingConflictError`/`ResourceBusyError` naming convention:
    `BedNotAvailableError` (fast-path status check failed, `admit_patient`),
    `BedConflictError` (the `EXCLUDE` constraint itself rejected the insert,
    `admit_patient`/`transfer_patient`), `AdmissionNotFoundError` (404,
    `discharge_patient`/`transfer_patient`), `AlreadyDischargedError` (409,
    `discharge_patient`/`transfer_patient`), `IllegalBedStatusTransition`
    (409, `set_bed_status`), `OTConflictError` (409, `schedule_ot` -- carries
    a `reason` distinguishing "surgeon busy in another OT" / "surgeon has a
    conflicting clinic appointment" / "room already booked"). `ResourceBusyError`
    is re-exported from `scheduling_engine` (not redefined) since it means
    exactly the same thing here (lock contention) -- see the import below.

(b) HOW THE ORIGINAL `stay_range` LOWER BOUND IS PRESERVED AT DISCHARGE --
    `discharge_patient`/`transfer_patient` need the admission's *original*
    start time to build the closed range literal `[start, discharge_time)`;
    they do NOT track it separately in a side column. Instead they read it
    back off the already-loaded `Admission.stay_range` (a psycopg2
    `Range` object once loaded from Postgres, whose `.lower` attribute is
    the original lower bound) via `_lower_bound_isoformat` below. This
    keeps the schema exactly as schema.sql defines it (no extra
    "admitted_at" column duplicating information already in the range) at
    the cost of one small helper that knows how psycopg2 represents a
    `tstzrange` server-side. Documented here per the task brief's explicit
    ask to flag this judgment call.

(c) OT SURGEON-CONFLICT ASYMMETRY -- `schedule_ot` gives the room dimension
    of an OT booking the same two-layer guarantee (Redis lock + `EXCLUDE`
    constraint) every other resource in this codebase gets, but the surgeon
    dimension only ever gets the app-level pre-check (step 2 below,
    performed while still holding the surgeon's lock). This is not a bug to
    fix quietly -- see `models/ward.py`'s module docstring point 3 and the
    PRP's design decision #3: Postgres cannot express a single exclusion
    constraint spanning `ot_schedules` and `appointments` (two different
    tables), so there is no DB-level backstop for a surgeon double-booked
    across two OTs, or between an OT slot and a clinic appointment, if the
    Redis lock is ever lost to a network partition or TTL race. A future
    schema change (a dedicated surgeon-busy-interval table with its own
    `EXCLUDE` constraint) would be the real fix; this module does not
    attempt to fake one with triggers.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.locking import LockAcquisitionError, lock_manager
from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit import record_audit_event
from app.models.ward import Admission, Bed, BedStatus, OTSchedule

# Re-exported so callers only need to import from this module for the whole
# ward/OT surface, same as `scheduling_engine.ResourceBusyError` is the one
# "lock contention" exception used across that module's whole public API.
from app.services.scheduling_engine import ResourceBusyError  # noqa: F401

logger = logging.getLogger(__name__)


class BedNotAvailableError(Exception):
    """Raised by `admit_patient`'s fast-path check when `Bed.status` is not
    `available` -- a UX nicety so the caller gets a clean error before the
    DB insert is even attempted. The `EXCLUDE` constraint below is what's
    actually authoritative for overlap; this check can be stale if
    `Bed.status` has drifted from reality (e.g. bypassed by direct DB
    manipulation), which is exactly why `BedConflictError` exists as a
    separate, DB-backed guarantee."""


class BedConflictError(Exception):
    """Raised when the `admissions` table's `EXCLUDE USING gist (bed_id
    WITH =, stay_range WITH &&)` constraint rejects an insert -- the
    authoritative guard against two overlapping admissions for one bed,
    independent of whatever `Bed.status` currently says."""


class AdmissionNotFoundError(Exception):
    """Raised when `discharge_patient`/`transfer_patient` is given an
    `admission_id` that does not exist. Callers turn this into a 404."""


class BedNotFoundError(Exception):
    """Raised when a `bed_id` passed to `set_bed_status` (or looked up as
    `admit_patient`'s target) does not exist. Callers turn this into a
    404."""


class AlreadyDischargedError(Exception):
    """Raised when `discharge_patient`/`transfer_patient` targets an
    `Admission` whose `discharged_at` is already set -- the same
    "don't allow a double-transition" discipline `lab_workflow.py`'s state
    machine uses. Callers turn this into a 409."""


class IllegalBedStatusTransition(Exception):
    """Raised by `set_bed_status` when `(current, requested)` is not one of
    the legal manual transitions. `occupied` is never a legal source or
    target here -- see module and `models/ward.py` docstrings. Callers turn
    this into a 409."""

    def __init__(self, *, current: BedStatus, requested: BedStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"illegal bed status transition: {current.value} -> {requested.value}")


class OTConflictError(Exception):
    """Raised when an OT booking cannot proceed -- either the app-level
    surgeon-availability pre-check found a conflict (`reason` is
    'surgeon_busy_ot' or 'surgeon_busy_appointment') or the room-only
    `EXCLUDE` constraint rejected the insert (`reason` is 'room_conflict').
    Carries `reason` so callers can surface a message that honestly
    distinguishes these cases rather than a single generic "OT slot
    unavailable" (see module docstring point (c))."""

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# --- Legal manual bed status transitions (mirrors lab_workflow.py's
# `_NEXT_STATUS` shape, at much smaller scope: a set of legal (current,
# requested) pairs rather than a linear chain, since this graph branches --
# `available` and `cleaning` both lead to `blocked`, and `blocked` only
# leads back to `available`). `occupied` never appears as a key or a value
# here -- see module docstring and `models/ward.py` point 2. ---------------
_LEGAL_BED_TRANSITIONS: set[tuple[BedStatus, BedStatus]] = {
    (BedStatus.cleaning, BedStatus.available),
    (BedStatus.available, BedStatus.blocked),
    (BedStatus.cleaning, BedStatus.blocked),
    (BedStatus.blocked, BedStatus.available),
}

# Appointment statuses that do NOT count as an active clinic booking for
# surgeon-conflict purposes -- identical filter to
# `scheduling_engine._overlap_exists`'s doctor-conflict check.
_INACTIVE_APPOINTMENT_STATUSES = (
    AppointmentStatus.cancelled,
    AppointmentStatus.no_show,
    AppointmentStatus.preempted,
)


def _lower_bound_isoformat(stay_range) -> str:
    """Extract the original lower bound of an already-loaded `stay_range`
    (a psycopg2 `Range` once read back from Postgres) as an ISO-8601
    string, for rebuilding a closed range literal at discharge/transfer.
    See module docstring point (b) for why this reads it back from the
    range itself rather than tracking a separate "admitted_at" column."""
    lower = stay_range.lower
    if isinstance(lower, str):
        return lower
    return lower.isoformat()


def _open_range_literal(start_time: datetime) -> str:
    return f"[{start_time.isoformat()},)"


def _closed_range_literal(start_iso: str, end_time: datetime) -> str:
    return f"[{start_iso},{end_time.isoformat()})"


def admit_patient(
    db: Session,
    *,
    patient_id: uuid.UUID,
    bed_id: uuid.UUID,
    start_time: datetime,
    admitted_by: uuid.UUID,
) -> Admission:
    """Admit a patient into a bed with an open-ended `stay_range`.

    Mirrors `scheduling_engine.book_appointment`'s shape: acquire the bed's
    lock, fast-path-check `Bed.status`, insert, catch `IntegrityError` from
    the `EXCLUDE` constraint as the authoritative guard, set `Bed.status =
    occupied` only on success, commit, audit-log.

    Raises:
        BedNotAvailableError: `Bed.status != available` (fast-path, see
            class docstring).
        BedConflictError: the `EXCLUDE` constraint rejected the insert
            (authoritative guard, catches drift/races the fast-path missed).
        ResourceBusyError: the Redis lock could not be acquired.
    """
    try:
        with lock_manager.acquire_all([f"bed:{bed_id}"]):
            bed = db.get(Bed, bed_id)
            if bed is None:
                raise BedNotFoundError(f"bed {bed_id} not found")
            if bed.status != BedStatus.available:
                raise BedNotAvailableError(f"bed {bed_id} is not available for admission")

            admission = Admission(
                patient_id=patient_id,
                bed_id=bed_id,
                stay_range=_open_range_literal(start_time),
                admitted_by=admitted_by,
            )
            db.add(admission)

            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                logger.warning("exclusion constraint rejected admission: %s", exc)
                raise BedConflictError(f"bed {bed_id} has an overlapping admission") from exc

            bed.status = BedStatus.occupied
            db.add(bed)

            record_audit_event(
                db,
                branch_id=None,
                actor_user_id=admitted_by,
                action="ward.admitted",
                resource_type="admission",
                resource_id=str(admission.id),
                metadata={"patient_id": str(patient_id), "bed_id": str(bed_id), "start_time": start_time.isoformat()},
            )
            db.commit()
            db.refresh(admission)
            return admission
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc


def discharge_patient(
    db: Session,
    *,
    admission_id: uuid.UUID,
    discharged_by: uuid.UUID,
    discharge_time: datetime | None = None,
) -> Admission:
    """Close an admission's `stay_range` and free its bed to `cleaning`.

    REVIEW-AGENT finding H1 (PRPs/ward-bed-ot-module-prp.md Phase 3), and a
    follow-up TEST-AGENT regression on the first attempted fix: the
    `discharged_at is not None` guard must be checked INSIDE the lock scope,
    against a row that has actually been refreshed from the database at
    that point -- not against the object loaded before the lock was
    requested. The original version checked once, before acquiring the
    lock, on a plain `db.get()` load; two near-simultaneous discharge/
    transfer calls for the SAME admission could both pass that check before
    either committed, and the second would silently overwrite the first's
    `stay_range`/`discharged_at` with no conflict ever raised.

    The first attempted fix re-ran `select(Admission)...with_for_update()`
    inside the lock, but on the SAME `Session` that already did the earlier
    unlocked `db.get()` -- SQLAlchemy's identity map returns that call's
    *same Python object* for a row already present in the session, without
    refreshing its attributes from the new query's result, so the "recheck"
    silently re-read the stale `discharged_at` it already had in memory
    (confirmed by TEST-AGENT: `refetched is pre_lock_obj` was `True`). The
    fix is `execution_options(populate_existing=True)`, which forces
    SQLAlchemy to overwrite that cached object's attributes from the fresh
    row the `SELECT ... FOR UPDATE` just locked, even though it's the same
    object identity. Only this line changed from the previous (broken) fix.

    Raises:
        AdmissionNotFoundError: no such admission (404).
        AlreadyDischargedError: `discharged_at` already set (409).
        ResourceBusyError: the Redis lock could not be acquired.
    """
    discharge_time = discharge_time if discharge_time is not None else datetime.now(timezone.utc)

    # One unlocked read just to learn which bed to lock (the lock key needs
    # `admission.bed_id` before a lock can even be requested) -- this is NOT
    # the authoritative "not already discharged" check; that happens below,
    # under the lock, against a row forcibly refreshed from the database.
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise AdmissionNotFoundError(f"admission {admission_id} not found")

    try:
        with lock_manager.acquire_all([f"bed:{admission.bed_id}"]):
            admission = db.execute(
                select(Admission)
                .where(Admission.id == admission_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if admission is None:
                raise AdmissionNotFoundError(f"admission {admission_id} not found")
            if admission.discharged_at is not None:
                raise AlreadyDischargedError(f"admission {admission_id} is already discharged")

            start_iso = _lower_bound_isoformat(admission.stay_range)
            admission.stay_range = _closed_range_literal(start_iso, discharge_time)
            admission.discharged_at = discharge_time
            db.add(admission)

            bed = db.get(Bed, admission.bed_id)
            assert bed is not None  # FK guarantees this; admission couldn't exist otherwise
            bed.status = BedStatus.cleaning
            db.add(bed)

            record_audit_event(
                db,
                branch_id=None,
                actor_user_id=discharged_by,
                action="ward.discharged",
                resource_type="admission",
                resource_id=str(admission.id),
                metadata={"discharge_time": discharge_time.isoformat()},
            )
            db.commit()
            db.refresh(admission)
            return admission
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc


def transfer_patient(
    db: Session,
    *,
    admission_id: uuid.UUID,
    new_bed_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> Admission:
    """Discharge the current bed and admit into `new_bed_id`, atomically.

    Both bed keys are locked in ONE `lock_manager.acquire_all([...])` call
    (never two sequential single-key acquisitions -- that would defeat the
    lock manager's built-in sorted-ordering deadlock prevention, see
    `core/locking.py`). The close-old / open-new sequence happens under
    that single lock scope and is committed exactly once: if the new
    admission's insert fails (e.g. `new_bed_id` is itself occupied), the
    whole transaction rolls back, including the old admission's close --
    there is no valid intermediate state where the old bed is freed but the
    new one wasn't claimed, or vice versa.

    REVIEW-AGENT finding H1 (PRPs/ward-bed-ot-module-prp.md Phase 3): the
    `discharged_at` guard is re-checked INSIDE the lock, against a row
    forcibly refreshed via `SELECT ... FOR UPDATE` +
    `execution_options(populate_existing=True)` -- see `discharge_patient`'s
    docstring for the exact race this closes (two near-simultaneous
    discharge/transfer calls for the same admission on the same `Session`,
    where a plain re-`SELECT` without `populate_existing` would have
    returned the same stale, already-cached Python object instead of a
    truly refreshed row).

    REVIEW-AGENT finding H2: `new_bed_id`'s `Bed.status` is now checked
    (`== available`) before the new admission is even constructed, the same
    fast-path `admit_patient` already had -- the original version relied
    solely on the `EXCLUDE` constraint here, which only rejects an
    *overlapping admission row*; a `blocked` (infection-control/maintenance
    override) or `cleaning` (not yet sanitized) bed has no active admission
    row at all, so nothing stopped a transfer landing a patient in one.

    Raises:
        AdmissionNotFoundError: no such admission (404).
        AlreadyDischargedError: the admission being transferred is already
            discharged (409) -- you cannot transfer a stay that has ended.
        BedNotAvailableError: `new_bed_id`'s status is not `available`
            (fast-path, same reasoning as `admit_patient`'s check).
        BedConflictError: `new_bed_id` has an overlapping admission
            (authoritative `EXCLUDE`-constraint guard).
        ResourceBusyError: the Redis lock could not be acquired.
    """
    # One unlocked read just to learn which beds to lock -- not the
    # authoritative check; see discharge_patient's docstring.
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise AdmissionNotFoundError(f"admission {admission_id} not found")

    old_bed_id = admission.bed_id
    now = datetime.now(timezone.utc)

    try:
        with lock_manager.acquire_all([f"bed:{old_bed_id}", f"bed:{new_bed_id}"]):
            admission = db.execute(
                select(Admission)
                .where(Admission.id == admission_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if admission is None:
                raise AdmissionNotFoundError(f"admission {admission_id} not found")
            if admission.discharged_at is not None:
                raise AlreadyDischargedError(f"admission {admission_id} is already discharged")

            new_bed = db.get(Bed, new_bed_id)
            if new_bed is None:
                raise BedNotFoundError(f"bed {new_bed_id} not found")
            if new_bed.status != BedStatus.available:
                raise BedNotAvailableError(f"bed {new_bed_id} is not available for transfer")

            # -- close the old admission (same as discharge_patient step 3) --
            start_iso = _lower_bound_isoformat(admission.stay_range)
            admission.stay_range = _closed_range_literal(start_iso, now)
            admission.discharged_at = now
            db.add(admission)

            old_bed = db.get(Bed, old_bed_id)
            assert old_bed is not None
            old_bed.status = BedStatus.cleaning
            db.add(old_bed)

            # -- open the new admission (same as admit_patient steps 2-4) --
            new_admission = Admission(
                patient_id=admission.patient_id,
                bed_id=new_bed_id,
                stay_range=_open_range_literal(now),
                admitted_by=actor_user_id,
            )
            db.add(new_admission)

            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                logger.warning("exclusion constraint rejected transfer: %s", exc)
                raise BedConflictError(f"bed {new_bed_id} has an overlapping admission") from exc

            # `new_bed` was already loaded and status-checked above, before the
            # insert -- no need to re-fetch it here.
            new_bed.status = BedStatus.occupied
            db.add(new_bed)

            record_audit_event(
                db,
                branch_id=None,
                actor_user_id=actor_user_id,
                action="ward.transferred",
                resource_type="admission",
                resource_id=str(admission.id),
                metadata={
                    "old_bed_id": str(old_bed_id),
                    "new_bed_id": str(new_bed_id),
                    "new_admission_id": str(new_admission.id),
                },
            )
            db.commit()
            db.refresh(new_admission)
            return new_admission
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc


def set_bed_status(
    db: Session,
    *,
    bed_id: uuid.UUID,
    requested_status: BedStatus,
    actor_user_id: uuid.UUID,
) -> Bed:
    """Manual bed status transition (cleaning->available, available->blocked,
    cleaning->blocked, blocked->available). `occupied` is never a legal
    source or target here -- see `_LEGAL_BED_TRANSITIONS` and module
    docstring.

    Raises:
        IllegalBedStatusTransition: `(current, requested)` is not in the
            legal transition set (409). This includes any attempt to move
            into or out of `occupied` via this function.
        ResourceBusyError: the Redis lock could not be acquired.
    """
    try:
        with lock_manager.acquire_all([f"bed:{bed_id}"]):
            bed = db.get(Bed, bed_id)
            if bed is None:
                raise BedNotFoundError(f"bed {bed_id} not found")

            current = bed.status
            if (current, requested_status) not in _LEGAL_BED_TRANSITIONS:
                raise IllegalBedStatusTransition(current=current, requested=requested_status)

            bed.status = requested_status
            db.add(bed)

            record_audit_event(
                db,
                branch_id=None,
                actor_user_id=actor_user_id,
                action="ward.bed_status_changed",
                resource_type="bed",
                resource_id=str(bed.id),
                metadata={"from": current.value, "to": requested_status.value},
            )
            db.commit()
            db.refresh(bed)
            return bed
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc


def schedule_ot(
    db: Session,
    *,
    room_id: uuid.UUID,
    patient_id: uuid.UUID,
    surgeon_id: uuid.UUID,
    start_time: datetime,
    duration_minutes: int,
    actor_user_id: uuid.UUID,
) -> OTSchedule:
    """Book an OT room + surgeon + patient for a time slot.

    Two-layer guarantee for the ROOM dimension only (Redis lock +
    `ot_schedules`' room-only `EXCLUDE` constraint) -- the SURGEON dimension
    gets ONLY the app-level pre-check in step 2 below, held under the same
    lock, with NO DB-level backstop, because Postgres cannot express a
    single exclusion constraint spanning `ot_schedules` and `appointments`
    (two separate tables). This is a real, documented limitation (PRP
    design decision #3, `models/ward.py` module docstring point 3) --if the
    Redis lock is ever lost (network partition, TTL race under an unusually
    slow transaction), a surgeon could theoretically be double-booked
    across two OTs or between an OT slot and a clinic appointment with
    nothing at the database level to stop it. A future schema change (a
    dedicated surgeon-busy-interval table with its own `EXCLUDE`
    constraint) would be the real fix; this function does not attempt to
    fake one with triggers.

    Raises:
        OTConflictError: `reason='surgeon_busy_ot'` (conflicting
            `OTSchedule` row for this surgeon), `reason='surgeon_busy_appointment'`
            (conflicting clinic `Appointment` for this surgeon, excluding
            cancelled/no_show/preempted), or `reason='room_conflict'` (the
            room-only `EXCLUDE` constraint rejected the insert).
        ResourceBusyError: the Redis lock could not be acquired.
    """
    end_time = start_time + timedelta(minutes=duration_minutes)
    range_literal = f"[{start_time.isoformat()},{end_time.isoformat()})"

    try:
        with lock_manager.acquire_all([f"room:{room_id}", f"surgeon:{surgeon_id}"]):
            # -- app-level surgeon-availability pre-check (design decision #3) --
            ot_conflict = db.execute(
                select(OTSchedule.id).where(
                    OTSchedule.surgeon_id == surgeon_id,
                    OTSchedule.time_range.op("&&")(range_literal),
                )
            ).first()
            if ot_conflict:
                raise OTConflictError(
                    reason="surgeon_busy_ot",
                    message=f"surgeon {surgeon_id} is already scheduled in another OT during this slot",
                )

            appointment_conflict = db.execute(
                select(Appointment.id).where(
                    Appointment.doctor_id == surgeon_id,
                    Appointment.status.notin_(_INACTIVE_APPOINTMENT_STATUSES),
                    Appointment.time_range.op("&&")(range_literal),
                )
            ).first()
            if appointment_conflict:
                raise OTConflictError(
                    reason="surgeon_busy_appointment",
                    message=f"surgeon {surgeon_id} has a conflicting clinic appointment during this slot",
                )

            ot_schedule = OTSchedule(
                room_id=room_id,
                patient_id=patient_id,
                surgeon_id=surgeon_id,
                time_range=range_literal,
            )
            db.add(ot_schedule)

            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                logger.warning("exclusion constraint rejected OT booking: %s", exc)
                raise OTConflictError(
                    reason="room_conflict",
                    message=f"room {room_id} is already booked during this slot",
                ) from exc

            record_audit_event(
                db,
                branch_id=None,
                actor_user_id=actor_user_id,
                action="ward.ot_scheduled",
                resource_type="ot_schedule",
                resource_id=str(ot_schedule.id),
                metadata={
                    "room_id": str(room_id),
                    "patient_id": str(patient_id),
                    "surgeon_id": str(surgeon_id),
                    "start_time": start_time.isoformat(),
                    "duration_minutes": duration_minutes,
                },
            )
            db.commit()
            db.refresh(ot_schedule)
            return ot_schedule
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc
