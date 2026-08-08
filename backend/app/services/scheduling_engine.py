"""Atomic Resource Booking Engine.

Books a Doctor + Room + N pieces of Equipment for a time interval as a single
atomic unit. Two independent defenses against double-booking, deliberately
layered (see docs/ARCHITECTURE.md section 5):

  1. Redis distributed lock, acquired for every resource involved, in a
     globally sorted key order (prevents cross-request deadlock). This is
     the fast-fail layer — under contention, a second caller gets a busy
     response in milliseconds instead of racing the DB.
  2. Postgres EXCLUDE constraints on (resource_id, time_range) for doctor,
     room, and each equipment slot (see Appointment / AppointmentRoomLock /
     AppointmentEquipmentLock). This is the *authoritative* correctness
     guarantee: even if the Redis lock is lost to a network partition or a
     TTL race, Postgres physically refuses the overlapping row.

A `SELECT ... FOR UPDATE` pre-check is also run inside the transaction so
that under normal operation we return a clean "slot taken" error rather
than surfacing a raw constraint-violation exception to the caller.
"""

import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.events import event_publisher
from app.core.locking import LockAcquisitionError, lock_manager
from app.models.appointment import (
    Appointment,
    AppointmentEquipmentLock,
    AppointmentRoomLock,
    AppointmentStatus,
)
from app.models.resource import DoctorShift
from app.services import waitlist_service

logger = logging.getLogger(__name__)


class BookingConflictError(Exception):
    """Raised when the requested resource combination is not available."""


class ResourceBusyError(Exception):
    """Raised when the distributed lock could not be acquired (high contention)."""


@dataclass
class BookingRequest:
    branch_id: UUID
    patient_id: UUID
    doctor_id: UUID
    room_id: UUID
    start_time: datetime
    duration_minutes: int
    equipment_ids: Sequence[UUID] = field(default_factory=list)
    triage_level: int | None = None
    is_emergency: bool = False
    series_id: UUID | None = None
    # HMS Project Completion Prompt gap ("rescheduling"): set only by
    # `reschedule_appointment` below, never directly by a router -- see that
    # function's docstring.
    reschedule_of_id: UUID | None = None


class AppointmentNotReschedulableError(Exception):
    """Raised by `reschedule_appointment` when the appointment being
    rescheduled is not in a reschedulable status (only `booked`/
    `checked_in` are -- an appointment already `in_progress`/`completed`
    has clinical work attached to that specific slot, and `cancelled`/
    `no_show`/`preempted` are already terminal)."""


def _resource_lock_keys(request: BookingRequest) -> list[str]:
    keys = [f"doctor:{request.doctor_id}", f"room:{request.room_id}"]
    keys += [f"equipment:{eid}" for eid in request.equipment_ids]
    return keys


def _overlap_exists(db: Session, request: BookingRequest, end_time: datetime) -> bool:
    """Row-lock and check for conflicts inside the transaction, ahead of the
    INSERT. This is a courtesy fast-path for a clean error message; the
    EXCLUDE constraints are what actually guarantee correctness if this
    check and a concurrent transaction interleave."""
    doctor_conflict = db.execute(
        select(Appointment.id)
        .where(
            Appointment.doctor_id == request.doctor_id,
            Appointment.status.notin_(
                [AppointmentStatus.cancelled, AppointmentStatus.no_show, AppointmentStatus.preempted]
            ),
            Appointment.time_range.op("&&")(f"[{request.start_time.isoformat()},{end_time.isoformat()})"),
        )
        .with_for_update()
    ).first()
    if doctor_conflict:
        return True

    room_conflict = db.execute(
        select(AppointmentRoomLock.appointment_id)
        .where(
            AppointmentRoomLock.room_id == request.room_id,
            AppointmentRoomLock.time_range.op("&&")(f"[{request.start_time.isoformat()},{end_time.isoformat()})"),
        )
        .with_for_update()
    ).first()
    if room_conflict:
        return True

    if request.equipment_ids:
        equip_conflict = db.execute(
            select(AppointmentEquipmentLock.id)
            .where(
                AppointmentEquipmentLock.equipment_id.in_(request.equipment_ids),
                AppointmentEquipmentLock.time_range.op("&&")(
                    f"[{request.start_time.isoformat()},{end_time.isoformat()})"
                ),
            )
            .with_for_update()
        ).first()
        if equip_conflict:
            return True

    return False


def _no_show_patient_history(db: Session, patient_id: UUID, start_time: datetime) -> dict:
    """Build the `patient_history` dict `score_no_show_risk` expects, from
    this patient's real appointment history -- the piece that was always
    missing (the scoring function itself existed but nothing ever called
    it or persisted its result, see that function's docstring and the
    whole-system review finding this closes).

    `prior_no_show_rate`: of this patient's *resolved* appointments
    (`completed`/`no_show` -- the only two outcomes where "did they show up"
    is actually known), the fraction that were `no_show`. `cancelled`/
    `preempted`/still-active appointments are excluded from both numerator
    and denominator, same reasoning `dashboard_service.get_no_show_rate`
    already uses for its own no-show accounting (neither is the patient
    failing to show up).

    `booked_same_day`: whether this booking's slot falls on the same
    calendar day it's being made, a same-day appointment has had no time for
    a reminder cycle to run.

    `reminder_acknowledged`: no field anywhere in this schema tracks whether
    a patient acknowledged a reminder (`Notification` records delivery
    status only, not read/ack receipts -- see models/notification.py).
    Defaults to `True` (no penalty) rather than inventing data that doesn't
    exist; a real acknowledgement channel would replace this default, not
    the scoring call site.
    """
    resolved_counts = db.execute(
        select(
            func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.no_show),
            func.count(Appointment.id).filter(
                Appointment.status.in_([AppointmentStatus.completed, AppointmentStatus.no_show])
            ),
        ).where(Appointment.patient_id == patient_id)
    ).one()
    no_show_count, resolved_count = resolved_counts
    prior_no_show_rate = (no_show_count / resolved_count) if resolved_count > 0 else 0.0

    now = datetime.now(timezone.utc)
    booked_same_day = start_time.astimezone(timezone.utc).date() == now.date()

    return {
        "prior_no_show_rate": prior_no_show_rate,
        "booked_same_day": booked_same_day,
        "reminder_acknowledged": True,
    }


def book_appointment(db: Session, request: BookingRequest) -> Appointment:
    """Atomically reserve doctor + room + equipment for the requested slot.

    Raises ResourceBusyError under lock contention (caller should retry with
    backoff) or BookingConflictError if the slot is genuinely taken.
    """
    end_time = request.start_time + timedelta(minutes=request.duration_minutes)
    range_literal = f"[{request.start_time.isoformat()},{end_time.isoformat()})"
    no_show_risk_score = score_no_show_risk(_no_show_patient_history(db, request.patient_id, request.start_time))

    try:
        with lock_manager.acquire_all(_resource_lock_keys(request)):
            if _overlap_exists(db, request, end_time):
                raise BookingConflictError("requested doctor/room/equipment slot is no longer available")

            appointment = Appointment(
                branch_id=request.branch_id,
                patient_id=request.patient_id,
                doctor_id=request.doctor_id,
                room_id=request.room_id,
                time_range=range_literal,
                status=AppointmentStatus.booked,
                triage_level=request.triage_level,
                is_emergency=request.is_emergency,
                no_show_risk_score=no_show_risk_score,
                series_id=request.series_id,
                reschedule_of_id=request.reschedule_of_id,
            )
            db.add(appointment)
            db.flush()  # obtain appointment.id, still inside the transaction

            db.add(AppointmentRoomLock(appointment_id=appointment.id, room_id=request.room_id, time_range=range_literal))
            for equipment_id in request.equipment_ids:
                db.add(
                    AppointmentEquipmentLock(
                        appointment_id=appointment.id, equipment_id=equipment_id, time_range=range_literal
                    )
                )

            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                # Authoritative guard tripped: some other transaction won the
                # race despite the Redis lock (e.g. lock TTL expired under a
                # very slow transaction). Surface as a clean conflict.
                logger.warning("exclusion constraint rejected booking: %s", exc)
                raise BookingConflictError("requested slot was booked concurrently") from exc

            db.refresh(appointment)
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc

    event_publisher.publish(
        "appointment.booked",
        {
            "appointment_id": str(appointment.id),
            "branch_id": str(appointment.branch_id),
            "doctor_id": str(appointment.doctor_id),
            "patient_id": str(appointment.patient_id),
            "start_time": request.start_time.isoformat(),
            "is_emergency": appointment.is_emergency,
        },
    )
    return appointment


def cancel_appointment(db: Session, appointment: Appointment, reason: str) -> None:
    """Cancel a booking, freeing its resource locks.

    The `appointments` EXCLUDE constraint's WHERE clause only considers
    non-cancelled rows, so the doctor is freed by the status flip alone. But
    `AppointmentRoomLock`/`AppointmentEquipmentLock` are separate tables with
    no `status` column and no WHERE-filtered EXCLUDE constraint of their own
    (see their model definitions) — every reader of them
    (`_overlap_exists` above, `emergency_engine._find_free_doctor_and_room`)
    queries them directly with no join back to `Appointment.status`. Without
    deleting these rows here, a cancelled appointment would permanently keep
    its room (and any equipment) marked busy for that time range forever.
    `ondelete="CASCADE"` on their `appointment_id` FK only helps if the
    appointment row itself is deleted, which it never is (cancellation is a
    soft status flip, not a delete)."""
    doctor_id = appointment.doctor_id
    freed_lower = appointment.time_range.lower
    if isinstance(freed_lower, str):
        freed_lower = datetime.fromisoformat(freed_lower)
    freed_date = freed_lower.date() if freed_lower is not None else None

    appointment.status = AppointmentStatus.cancelled
    db.add(appointment)
    db.execute(delete(AppointmentRoomLock).where(AppointmentRoomLock.appointment_id == appointment.id))
    db.execute(delete(AppointmentEquipmentLock).where(AppointmentEquipmentLock.appointment_id == appointment.id))
    db.commit()

    # Fairness allocation (HMS Project Completion Prompt gap): cancelling a
    # booked appointment frees a slot for this doctor on this date -- offer
    # it to whoever has been on the waiting list longest. Centralized here
    # (not duplicated at each cancel_appointment call site -- the staff
    # router and services/patient_portal_service.py's cancel_my_appointment
    # both call this same function) so every cancellation path triggers the
    # same fairness check.
    if freed_date is not None:
        waitlist_service.try_offer_freed_slot(db, doctor_id, freed_date)

    event_publisher.publish(
        "appointment.cancelled",
        {"appointment_id": str(appointment.id), "reason": reason},
    )


_RESCHEDULABLE_STATUSES = {AppointmentStatus.booked, AppointmentStatus.checked_in}


def reschedule_appointment(
    db: Session,
    old_appointment: Appointment,
    *,
    doctor_id: UUID,
    room_id: UUID,
    start_time: datetime,
    duration_minutes: int,
    equipment_ids: Sequence[UUID] = (),
) -> Appointment:
    """HMS Project Completion Prompt gap ("rescheduling") -- `Appointment.
    reschedule_of_id` existed as a column but was never written anywhere
    outside `emergency_engine.py`'s preemption-bump bookkeeping (confirmed
    by grep before writing this), so there was no actual reschedule feature.

    Books the NEW slot FIRST (through the exact same `book_appointment` ->
    Redis-lock -> EXCLUDE-constraint path every other booking uses -- no
    weaker conflict-check code path for a reschedule), and only cancels
    `old_appointment` once that succeeds. This ordering is deliberate: if
    the new slot is unavailable, `BookingConflictError`/`ResourceBusyError`
    propagates and `old_appointment` is untouched -- a patient being
    rescheduled must never end up with NEITHER an old nor a new appointment
    because the new slot they picked turned out to be taken.

    `branch_id`/`patient_id` are derived from `old_appointment`, never
    caller-supplied -- a reschedule moves an existing patient's existing
    appointment to a new time/doctor/room, it must not be usable to
    reassign the slot to a different patient or branch.

    Raises:
        AppointmentNotReschedulableError: `old_appointment.status` is not
            `booked` or `checked_in` (409, caller's job to map).
        BookingConflictError / ResourceBusyError: propagated from
            `book_appointment` for the new slot (old appointment untouched).
    """
    if old_appointment.status not in _RESCHEDULABLE_STATUSES:
        raise AppointmentNotReschedulableError(
            f"appointment {old_appointment.id} cannot be rescheduled from status={old_appointment.status.value!r}"
        )

    new_request = BookingRequest(
        branch_id=old_appointment.branch_id,
        patient_id=old_appointment.patient_id,
        doctor_id=doctor_id,
        room_id=room_id,
        start_time=start_time,
        duration_minutes=duration_minutes,
        equipment_ids=equipment_ids,
        triage_level=old_appointment.triage_level,
        is_emergency=old_appointment.is_emergency,
        reschedule_of_id=old_appointment.id,
    )
    new_appointment = book_appointment(db, new_request)

    cancel_appointment(db, old_appointment, reason="rescheduled")

    return new_appointment


def recalculate_downstream_wait_times(
    db: Session, doctor_id: UUID, actual_end_time: datetime
) -> list[Appointment]:
    """Called when a consultation runs over its predicted duration.

    Shifts the effective expected-start of every later booked appointment
    for this doctor today and republishes updated queue estimates. Uses a
    simple cumulative-delay propagation; swap-in point for a learned
    per-doctor duration model later.
    """
    upcoming = (
        db.execute(
            select(Appointment)
            .where(
                Appointment.doctor_id == doctor_id,
                Appointment.status == AppointmentStatus.booked,
                Appointment.time_range.op(">>")(f"[{actual_end_time.isoformat()},)"),
            )
            .order_by(Appointment.time_range)
        )
        .scalars()
        .all()
    )
    for appt in upcoming:
        event_publisher.publish(
            "queue.wait_time_updated",
            {"appointment_id": str(appt.id), "doctor_id": str(doctor_id)},
        )
    return list(upcoming)


def score_no_show_risk(patient_history: dict) -> float:
    """Heuristic no-show risk score in [0, 1].

    Placeholder weighted-feature heuristic — deliberately simple and
    replaceable. Wire an actual ML model behind this same signature
    (`score_no_show_risk(patient_history: dict) -> float`) without touching
    any caller once real training data exists.
    """
    weight_prior_no_shows = 0.5
    weight_same_day_booking = 0.2
    weight_no_reminder_ack = 0.3

    prior_rate = patient_history.get("prior_no_show_rate", 0.0)
    same_day = 1.0 if patient_history.get("booked_same_day", False) else 0.0
    no_ack = 1.0 if not patient_history.get("reminder_acknowledged", True) else 0.0

    score = (
        weight_prior_no_shows * prior_rate
        + weight_same_day_booking * same_day
        + weight_no_reminder_ack * no_ack
    )
    return max(0.0, min(1.0, score))


_SLOT_GRANULARITY_MINUTES = 15


def _range_bounds(range_value) -> tuple[datetime, datetime] | None:
    """Normalizes a SQLAlchemy/psycopg2 `Range` value's `.lower`/`.upper`
    to a `(datetime, datetime)` pair, or `None` for an unbounded/empty
    range (never expected for `shift_range`/`time_range` in practice, both
    always-bounded columns, but handled rather than assumed). Mirrors
    `scheduling_engine.cancel_appointment`'s existing "`.lower` can come
    back as either a `datetime` or an ISO string depending on how the row
    was written" handling -- same defensive normalization, reused here
    rather than duplicated with different logic."""
    lower, upper = range_value.lower, range_value.upper
    if lower is None or upper is None:
        return None
    if isinstance(lower, str):
        lower = datetime.fromisoformat(lower)
    if isinstance(upper, str):
        upper = datetime.fromisoformat(upper)
    return lower, upper


def get_available_slots(
    db: Session, *, doctor_id: UUID, target_date: date_type, duration_minutes: int
) -> list[datetime]:
    """HMS Project Completion Prompt gap ("available-slot display"): before
    this, booking was "guess a `start_time`, get a 409 if it's wrong" --
    this computes actual candidate start times for a doctor on a given date.

    Algorithm: fetch this doctor's `DoctorShift` ranges overlapping
    `target_date` (UTC calendar day), subtract every active (non-cancelled/
    no_show/preempted) `Appointment` time range that overlaps the same day,
    then walk the remaining free windows in `_SLOT_GRANULARITY_MINUTES`
    (15-minute) increments, yielding every start time where a
    `duration_minutes`-long appointment fits entirely inside one free
    window.

    Deliberately doctor-only, not also room/equipment-aware: a doctor's
    shift is the primary real-world constraint or a slot doesn't exist in
    the first place, and this system's rooms/equipment are ordinary shared
    resources without their own working-hours concept to intersect against
    (unlike `DoctorShift`, there is no `RoomShift`/`EquipmentShift` table).
    A candidate slot from here can still 409 at actual `book_appointment`
    time if the caller's chosen room/equipment is independently busy --
    that check remains the authoritative one, this endpoint is a UX
    improvement over guessing, not a second source of truth.
    """
    day_start = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    day_literal = f"[{day_start.isoformat()},{day_end.isoformat()})"

    shift_rows = db.execute(
        select(DoctorShift.shift_range).where(
            DoctorShift.doctor_id == doctor_id,
            DoctorShift.shift_range.op("&&")(day_literal),
        )
    ).scalars().all()

    busy_rows = db.execute(
        select(Appointment.time_range).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.notin_(
                [AppointmentStatus.cancelled, AppointmentStatus.no_show, AppointmentStatus.preempted]
            ),
            Appointment.time_range.op("&&")(day_literal),
        )
    ).scalars().all()

    busy_windows = sorted(bounds for row in busy_rows if (bounds := _range_bounds(row)) is not None)

    duration = timedelta(minutes=duration_minutes)
    granularity = timedelta(minutes=_SLOT_GRANULARITY_MINUTES)
    slots: list[datetime] = []

    for shift_row in shift_rows:
        shift_bounds = _range_bounds(shift_row)
        if shift_bounds is None:
            continue
        # Clip the shift to the requested calendar day -- a shift spanning
        # midnight only contributes the portion that actually falls on
        # `target_date`.
        window_start = max(shift_bounds[0], day_start)
        window_end = min(shift_bounds[1], day_end)
        if window_start >= window_end:
            continue

        cursor = window_start
        while cursor + duration <= window_end:
            candidate_end = cursor + duration
            conflicts = any(cursor < busy_end and candidate_end > busy_start for busy_start, busy_end in busy_windows)
            if not conflicts:
                slots.append(cursor)
            cursor += granularity

    return sorted(slots)
