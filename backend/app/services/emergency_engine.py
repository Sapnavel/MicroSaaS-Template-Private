"""Emergency Dynamic Preemption Engine.

Triage Level 1/2 patients do not wait for a free slot. This module first
tries to find a genuinely free doctor+room(+equipment) combination within
the clinically acceptable window for that triage level; if none exists, it
preempts the lowest-cost currently-booked non-critical appointment, atomically
swaps the emergency case into that resource combination, and hands the
bumped patient off to the async reschedule pipeline via an event — the ER
flow never blocks on notification/rebooking latency.

See docs/ARCHITECTURE.md section 6 for the algorithm summary.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from app.models.resource import Doctor, DoctorShift, Room
from app.models.triage import TriageLevel
from app.services.scheduling_engine import (
    BookingConflictError,
    BookingRequest,
    ResourceBusyError,
    book_appointment,
)

logger = logging.getLogger(__name__)


class NoCapacityError(Exception):
    """Raised when neither a free slot nor a viable victim to preempt exists."""


@dataclass
class EmergencyRequest:
    branch_id: UUID
    patient_id: UUID
    specialty_id: int
    triage_level: int  # 1 or 2 to actually trigger preemption eligibility
    room_type: str = "consultation"
    equipment_ids: list[UUID] | None = None
    duration_minutes: int = 20


def _on_shift_doctor_ids(db: Session, branch_id: UUID, specialty_id: int, at: datetime) -> list[UUID]:
    rows = db.execute(
        select(Doctor.id)
        .join(DoctorShift, DoctorShift.doctor_id == Doctor.id)
        .where(
            Doctor.branch_id == branch_id,
            Doctor.specialty_id == specialty_id,
            Doctor.is_active.is_(True),
            DoctorShift.shift_range.op("@>")(at.isoformat()),
        )
    ).scalars().all()
    return list(rows)


def _find_free_slot(
    db: Session, request: EmergencyRequest, doctor_ids: list[UUID], window_end: datetime
) -> tuple[UUID, UUID] | None:
    """Look for a doctor+room combination with no active appointment
    overlapping [now, window_end). Returns (doctor_id, room_id) or None."""
    now = datetime.utcnow()
    if not doctor_ids:
        return None

    busy_doctor_ids = set(
        db.execute(
            select(Appointment.doctor_id).where(
                Appointment.doctor_id.in_(doctor_ids),
                Appointment.status.notin_(
                    [AppointmentStatus.cancelled, AppointmentStatus.no_show, AppointmentStatus.preempted]
                ),
                Appointment.time_range.op("&&")(f"[{now.isoformat()},{window_end.isoformat()})"),
            )
        ).scalars()
    )
    free_doctor_ids = [d for d in doctor_ids if d not in busy_doctor_ids]
    if not free_doctor_ids:
        return None

    room = db.execute(
        select(Room.id)
        .where(Room.branch_id == request.branch_id, Room.room_type == request.room_type, Room.is_active.is_(True))
        .where(
            ~select(AppointmentRoomLock.appointment_id)
            .where(
                AppointmentRoomLock.room_id == Room.id,
                AppointmentRoomLock.time_range.op("&&")(f"[{now.isoformat()},{window_end.isoformat()})"),
            )
            .exists()
        )
        .limit(1)
    ).scalar()
    if room is None:
        return None

    return free_doctor_ids[0], room


def _select_victim(db: Session, doctor_ids: list[UUID], window_end: datetime) -> Appointment | None:
    """Pick the lowest-cost currently-booked non-critical appointment to bump.

    Cost model (lower = safer to bump):
      - victim triage_level: NULL/routine treated as level 5 (safest to bump)
      - proximity to now: appointments further out cost less to bump
      - already-bumped patients cost more (avoid starving the same patient
        across repeated emergencies) — approximated via reschedule_of_id chain
    """
    now = datetime.utcnow()
    candidates = (
        db.execute(
            select(Appointment)
            .where(
                Appointment.doctor_id.in_(doctor_ids),
                Appointment.status.in_([AppointmentStatus.booked, AppointmentStatus.checked_in]),
                Appointment.is_emergency.is_(False),
                Appointment.time_range.op("&&")(f"[{now.isoformat()},{window_end.isoformat()})"),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None

    def cost(appt: Appointment) -> float:
        triage_cost = (appt.triage_level or 5) / 5.0  # higher triage number -> lower cost to bump
        already_bumped_penalty = 0.5 if appt.reschedule_of_id is not None else 0.0
        return triage_cost - already_bumped_penalty

    return min(candidates, key=cost)


def preempt_and_book(db: Session, request: EmergencyRequest) -> Appointment:
    """Entry point: book an emergency case, preempting a lower-priority
    booking if no free capacity exists. Raises NoCapacityError only if the
    entire department has no free or preemptable resource at all (should be
    operationally rare and indicates true overload, not a bug)."""
    triage = db.get(TriageLevel, request.triage_level)
    if triage is None or not triage.preempts:
        raise ValueError(f"triage level {request.triage_level} is not eligible for preemption")

    now = datetime.utcnow()
    window_end = now + timedelta(minutes=triage.max_wait_minutes or 10)
    doctor_ids = _on_shift_doctor_ids(db, request.branch_id, request.specialty_id, now)
    if not doctor_ids:
        raise NoCapacityError("no on-shift doctor for this specialty")

    equipment_ids = request.equipment_ids or []

    # --- Path 1: a genuinely free doctor+room combo exists -----------------
    # book_appointment() does its own locking/transaction atomically, so we
    # deliberately hold no lock here — nesting would deadlock against
    # ourselves (our ResourceLock is not reentrant). If the slot we saw as
    # free gets taken between this check and the booking call, book_appointment
    # raises BookingConflictError and we fall through to the preemption path
    # below instead of failing the emergency request outright.
    free = _find_free_slot(db, request, doctor_ids, window_end)
    if free is not None:
        doctor_id, room_id = free
        try:
            return book_appointment(
                db,
                BookingRequest(
                    branch_id=request.branch_id,
                    patient_id=request.patient_id,
                    doctor_id=doctor_id,
                    room_id=room_id,
                    start_time=now,
                    duration_minutes=request.duration_minutes,
                    equipment_ids=equipment_ids,
                    triage_level=request.triage_level,
                    is_emergency=True,
                ),
            )
        except BookingConflictError:
            logger.info("free slot for doctor=%s vanished before booking; falling back to preemption", doctor_id)

    # --- Path 2: preempt the lowest-cost booked non-critical appointment ---
    # SELECT ... FOR UPDATE SKIP LOCKED lets concurrent emergency requests
    # pick different victims without blocking on each other.
    victim = _select_victim(db, doctor_ids, window_end)
    if victim is None:
        raise NoCapacityError("no free or preemptable slot available for this specialty")

    # Scope the distributed lock tightly to just the resources this specific
    # swap will mutate (not every on-shift doctor), for both correctness
    # (no reentrancy risk) and throughput (unrelated bookings aren't blocked).
    lock_keys = sorted({f"doctor:{victim.doctor_id}", f"room:{victim.room_id}"} | {f"equipment:{e}" for e in equipment_ids})

    try:
        with lock_manager.acquire_all(lock_keys):
            # Re-fetch under FOR UPDATE now that we hold the distributed lock
            # too, in case the row changed between selection and lock acquisition.
            victim = db.execute(
                select(Appointment).where(Appointment.id == victim.id).with_for_update()
            ).scalar_one()
            if victim.status not in (AppointmentStatus.booked, AppointmentStatus.checked_in):
                raise NoCapacityError("selected victim appointment is no longer preemptable; retry")

            victim.status = AppointmentStatus.preempted
            db.add(victim)
            db.flush()

            end_time = now + timedelta(minutes=request.duration_minutes)
            range_literal = f"[{now.isoformat()},{end_time.isoformat()})"
            emergency_appt = Appointment(
                branch_id=request.branch_id,
                patient_id=request.patient_id,
                doctor_id=victim.doctor_id,
                room_id=victim.room_id,
                time_range=range_literal,
                status=AppointmentStatus.booked,
                triage_level=request.triage_level,
                is_emergency=True,
            )
            db.add(emergency_appt)
            db.flush()

            victim.preempted_by_appointment_id = emergency_appt.id
            db.add(victim)
            db.add(
                AppointmentRoomLock(
                    appointment_id=emergency_appt.id, room_id=victim.room_id, time_range=range_literal
                )
            )
            for equipment_id in equipment_ids:
                db.add(
                    AppointmentEquipmentLock(
                        appointment_id=emergency_appt.id, equipment_id=equipment_id, time_range=range_literal
                    )
                )

            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                logger.warning("preemption booking rejected by exclusion constraint: %s", exc)
                raise BookingConflictError("resource became unavailable during preemption") from exc

            db.refresh(emergency_appt)
    except LockAcquisitionError as exc:
        raise ResourceBusyError(str(exc)) from exc

    # Off the hot path: hand the bumped patient to the async reschedule
    # pipeline. A consumer (notification_service, stubbed) offers the next
    # available slot and notifies the patient — the ER flow above has
    # already returned by the time this is processed.
    event_publisher.publish(
        "appointment.preempted",
        {
            "victim_appointment_id": str(victim.id),
            "victim_patient_id": str(victim.patient_id),
            "preempted_by_appointment_id": str(emergency_appt.id),
            "doctor_id": str(victim.doctor_id),
            "triage_level": request.triage_level,
        },
    )
    event_publisher.publish(
        "appointment.booked",
        {
            "appointment_id": str(emergency_appt.id),
            "branch_id": str(emergency_appt.branch_id),
            "doctor_id": str(emergency_appt.doctor_id),
            "patient_id": str(emergency_appt.patient_id),
            "is_emergency": True,
            "triage_level": request.triage_level,
        },
    )
    return emergency_appt
