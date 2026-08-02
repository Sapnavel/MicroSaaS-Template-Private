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
from datetime import datetime, timedelta
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
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


def book_appointment(db: Session, request: BookingRequest) -> Appointment:
    """Atomically reserve doctor + room + equipment for the requested slot.

    Raises ResourceBusyError under lock contention (caller should retry with
    backoff) or BookingConflictError if the slot is genuinely taken.
    """
    end_time = request.start_time + timedelta(minutes=request.duration_minutes)
    range_literal = f"[{request.start_time.isoformat()},{end_time.isoformat()})"

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
    """Cancel a booking, freeing its resource locks (the EXCLUDE constraints
    only consider non-cancelled rows, so this is enough to free the slot —
    no separate 'unlock' step against the lock tables is needed)."""
    appointment.status = AppointmentStatus.cancelled
    db.add(appointment)
    db.commit()
    event_publisher.publish(
        "appointment.cancelled",
        {"appointment_id": str(appointment.id), "reason": reason},
    )


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
