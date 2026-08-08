"""Tests for `services/emergency_engine.preempt_and_book` (routers/emergency.py
`POST /api/v1/emergency/book`).

No test file for this module existed before this one -- the preemption path
(Path 2 in `preempt_and_book`) had zero automated coverage, which is exactly
how the following bug went unnoticed: preempting a victim appointment
flipped `victim.status` to `preempted` but never deleted the victim's
`AppointmentRoomLock`/`AppointmentEquipmentLock` rows (those tables have no
`status` column of their own). The new emergency appointment then tried to
insert its own room lock for the *same room_id* at an *overlapping time*
(the victim was selected specifically because its slot overlaps the
emergency window) -- colliding with its own stale lock row on the
`appointment_room_locks` EXCLUDE constraint, so preemption would fail every
time it was actually exercised. Fixed in `emergency_engine.py` alongside the
identical bug in `scheduling_engine.cancel_appointment` (see
test_concurrency.py's cancel-then-rebook test for that half).
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.appointment import Appointment, AppointmentRoomLock, AppointmentStatus
from app.models.patient import Patient
from app.models.resource import DoctorShift
from app.services.emergency_engine import EmergencyRequest, NoCapacityError, preempt_and_book


def _make_victim_appointment(db, *, branch_id, doctor_id, room_id, patient_id) -> Appointment:
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=5)
    end = now + timedelta(minutes=15)
    appt = Appointment(
        branch_id=branch_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        room_id=room_id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    db.add(AppointmentRoomLock(appointment_id=appt.id, room_id=room_id, time_range=appt.time_range))
    db.commit()
    return appt


def _make_shift_covering_now(db, doctor_id) -> None:
    now = datetime.now(timezone.utc)
    shift = DoctorShift(
        doctor_id=doctor_id,
        shift_range=f"[{(now - timedelta(hours=1)).isoformat()},{(now + timedelta(hours=8)).isoformat()})",
    )
    db.add(shift)
    db.commit()


def _make_emergency_patient(db) -> Patient:
    p = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Emergency Patient",
        dob=datetime(1990, 1, 1).date(),
        sex="M",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_preempt_and_book_bumps_victim_and_frees_its_room_lock(
    db, redis_client, branch, doctor_record, specialty, room, patient
):
    """The only on-shift doctor for this specialty is already fully booked in
    the only matching room, so `preempt_and_book` must fall through to the
    preemption path (Path 2) rather than finding a free slot."""
    _make_shift_covering_now(db, doctor_record.id)
    victim = _make_victim_appointment(
        db, branch_id=branch.id, doctor_id=doctor_record.id, room_id=room.id, patient_id=patient.id
    )
    emergency_patient = _make_emergency_patient(db)

    request = EmergencyRequest(
        branch_id=branch.id,
        patient_id=emergency_patient.id,
        specialty_id=specialty.id,
        triage_level=1,
        room_type=room.room_type,
    )

    emergency_appt = preempt_and_book(db, request)

    assert emergency_appt.is_emergency is True
    assert emergency_appt.doctor_id == doctor_record.id
    assert emergency_appt.room_id == room.id
    assert emergency_appt.patient_id == emergency_patient.id

    db.refresh(victim)
    assert victim.status == AppointmentStatus.preempted
    assert victim.preempted_by_appointment_id == emergency_appt.id

    # The room lock table must show exactly the new appointment's lock --
    # the victim's stale row must be gone, not merely superseded.
    lock_appointment_ids = set(
        db.execute(
            select(AppointmentRoomLock.appointment_id).where(AppointmentRoomLock.room_id == room.id)
        ).scalars()
    )
    assert lock_appointment_ids == {emergency_appt.id}


def test_preempt_and_book_no_capacity_raises(db, redis_client, branch, specialty, room):
    """No on-shift doctor at all for this specialty -> a clean domain error,
    not an unhandled exception."""
    emergency_patient = _make_emergency_patient(db)
    request = EmergencyRequest(
        branch_id=branch.id,
        patient_id=emergency_patient.id,
        specialty_id=specialty.id,
        triage_level=1,
        room_type=room.room_type,
    )

    try:
        preempt_and_book(db, request)
        raise AssertionError("expected NoCapacityError")
    except NoCapacityError:
        pass
