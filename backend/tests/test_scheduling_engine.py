"""Tests for `scheduling_engine.book_appointment`'s no-show risk scoring
(HMS Project Completion Prompt gap: `score_no_show_risk` existed as a pure
function with zero callers, and `Appointment.no_show_risk_score` was NULL
for every row in the codebase -- see dashboard_service.py's
`no_show_risk_score_note`, now stale). `book_appointment` is exercised
directly (not through the router) since these tests only care about the
persisted score, not HTTP/auth plumbing already covered by
test_appointments_router.py / test_concurrency.py.
"""

from datetime import date, datetime, timedelta, timezone

from app.models.appointment import Appointment, AppointmentStatus
from app.models.resource import DoctorShift
from app.services.scheduling_engine import BookingRequest, book_appointment, get_available_slots


def _make_resolved_appointment(db, *, branch_id, doctor_id, room_id, patient_id, status, days_ago) -> Appointment:
    start = datetime.now(timezone.utc) - timedelta(days=days_ago)
    end = start + timedelta(minutes=20)
    appt = Appointment(
        branch_id=branch_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        room_id=room_id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=status,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


def test_book_appointment_scores_no_show_risk_from_prior_history(
    redis_client, db, branch, doctor_record, other_doctor_record, room, patient
):
    """One prior `completed`, one prior `no_show` (against a different
    doctor/time so the EXCLUDE constraint doesn't collide with the new
    booking) -> prior_no_show_rate = 0.5. Booking for later today ->
    booked_same_day = True. No override on reminder_acknowledged -> default
    True (no penalty). Expected score: 0.5*0.5 + 0.2*1.0 + 0.3*0.0 = 0.45."""
    _make_resolved_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.completed,
        days_ago=30,
    )
    _make_resolved_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.no_show,
        days_ago=15,
    )

    start_time = datetime.now(timezone.utc) + timedelta(hours=2)
    request = BookingRequest(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        start_time=start_time,
        duration_minutes=20,
    )
    appointment = book_appointment(db, request)

    assert appointment.no_show_risk_score is not None
    assert abs(float(appointment.no_show_risk_score) - 0.45) < 0.01


def test_book_appointment_scores_zero_for_patient_with_no_history(
    redis_client, db, branch, doctor_record, room, patient
):
    """No prior resolved appointments, booked several days out (not
    same-day) -> every weighted term is 0."""
    start_time = datetime.now(timezone.utc) + timedelta(days=5)
    request = BookingRequest(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        start_time=start_time,
        duration_minutes=20,
    )
    appointment = book_appointment(db, request)

    assert appointment.no_show_risk_score is not None
    assert abs(float(appointment.no_show_risk_score) - 0.0) < 0.001


# ---------------------------------------------------------------------------
# get_available_slots -- HMS Project Completion Prompt gap ("available-slot
# display"). Fixed calendar date (not "now"-relative) so returned slot
# times can be asserted exactly.
# ---------------------------------------------------------------------------

_SLOT_TEST_DATE = date(2030, 1, 15)


def _make_shift(db, doctor_id, *, start_hour: int, end_hour: int) -> DoctorShift:
    start = datetime(2030, 1, 15, start_hour, 0, tzinfo=timezone.utc)
    end = datetime(2030, 1, 15, end_hour, 0, tzinfo=timezone.utc)
    shift = DoctorShift(doctor_id=doctor_id, shift_range=f"[{start.isoformat()},{end.isoformat()})")
    db.add(shift)
    db.commit()
    return shift


def test_get_available_slots_no_shift_returns_empty(db, doctor_record):
    slots = get_available_slots(
        db, doctor_id=doctor_record.id, target_date=_SLOT_TEST_DATE, duration_minutes=30
    )
    assert slots == []


def test_get_available_slots_empty_shift_no_busy_appointments(db, doctor_record):
    """09:00-11:00 shift, no bookings, 30-minute duration, 15-minute
    granularity -> every 15-minute-aligned start whose 30-minute span still
    fits before 11:00: 09:00, 09:15, ..., 10:30 (7 slots)."""
    _make_shift(db, doctor_record.id, start_hour=9, end_hour=11)

    slots = get_available_slots(
        db, doctor_id=doctor_record.id, target_date=_SLOT_TEST_DATE, duration_minutes=30
    )

    expected = [
        datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * i) for i in range(7)
    ]
    assert slots == expected


def test_get_available_slots_excludes_slots_overlapping_a_busy_appointment(
    db, branch, doctor_record, room, patient
):
    """Same 09:00-11:00 shift, but a 10:00-10:30 booked appointment removes
    every candidate slot whose 30-minute span would overlap it -- 09:45,
    10:00, and 10:15 are excluded (each overlaps [10:00,10:30)); 09:00,
    09:15, 09:30 (ends exactly at 10:00, no overlap), and 10:30 (starts
    exactly when the busy slot ends, no overlap) remain."""
    _make_shift(db, doctor_record.id, start_hour=9, end_hour=11)
    busy_start = datetime(2030, 1, 15, 10, 0, tzinfo=timezone.utc)
    busy_end = datetime(2030, 1, 15, 10, 30, tzinfo=timezone.utc)
    db.add(
        Appointment(
            branch_id=branch.id,
            patient_id=patient.id,
            doctor_id=doctor_record.id,
            room_id=room.id,
            time_range=f"[{busy_start.isoformat()},{busy_end.isoformat()})",
            status=AppointmentStatus.booked,
        )
    )
    db.commit()

    slots = get_available_slots(
        db, doctor_id=doctor_record.id, target_date=_SLOT_TEST_DATE, duration_minutes=30
    )

    expected = [
        datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc),
        datetime(2030, 1, 15, 9, 15, tzinfo=timezone.utc),
        datetime(2030, 1, 15, 9, 30, tzinfo=timezone.utc),
        datetime(2030, 1, 15, 10, 30, tzinfo=timezone.utc),
    ]
    assert slots == expected


def test_get_available_slots_ignores_cancelled_appointments(db, branch, doctor_record, room, patient):
    """A `cancelled` appointment must not block a slot -- it's freed."""
    _make_shift(db, doctor_record.id, start_hour=9, end_hour=10)
    busy_start = datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc)
    busy_end = datetime(2030, 1, 15, 9, 30, tzinfo=timezone.utc)
    db.add(
        Appointment(
            branch_id=branch.id,
            patient_id=patient.id,
            doctor_id=doctor_record.id,
            room_id=room.id,
            time_range=f"[{busy_start.isoformat()},{busy_end.isoformat()})",
            status=AppointmentStatus.cancelled,
        )
    )
    db.commit()

    slots = get_available_slots(
        db, doctor_id=doctor_record.id, target_date=_SLOT_TEST_DATE, duration_minutes=30
    )

    assert datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc) in slots


def test_get_available_slots_duration_longer_than_shift_returns_empty(db, doctor_record):
    """A 20-minute shift can never fit a 30-minute requested appointment."""
    start = datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    db.add(DoctorShift(doctor_id=doctor_record.id, shift_range=f"[{start.isoformat()},{end.isoformat()})"))
    db.commit()

    slots = get_available_slots(
        db, doctor_id=doctor_record.id, target_date=_SLOT_TEST_DATE, duration_minutes=30
    )
    assert slots == []
