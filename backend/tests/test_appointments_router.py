"""Integration tests for `GET /api/v1/appointments`
(routers/appointments.py, services/appointment_service.py) -- the frontend
UUID-to-dropdown conversion follow-up's backend phase. Feeds the queue
check-in dropdown (services/queue_service.check_in's appointment-linked
path). POST/DELETE on this router are covered elsewhere (this router had no
prior test file); this file is only about the new GET.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.appointment import Appointment, AppointmentStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_appointment(db, *, branch_id, doctor_id, room_id, patient_id, status, start) -> Appointment:
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


# ---------------------------------------------------------------------------
# GET /api/v1/appointments
# ---------------------------------------------------------------------------


def test_list_appointments_200_keeps_active_excludes_terminal(
    client, db, front_desk_user, staff_password, branch, doctor_record, other_doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    booked = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )
    checked_in = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.checked_in,
        start=now + timedelta(hours=1),
    )
    in_progress = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.in_progress,
        start=now + timedelta(hours=2),
    )
    cancelled = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.cancelled,
        start=now,
    )
    completed = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.completed,
        start=now + timedelta(hours=1),
    )
    no_show = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.no_show,
        start=now + timedelta(hours=2),
    )
    preempted = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.preempted,
        start=now + timedelta(hours=3),
    )

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.get("/api/v1/appointments", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert {str(booked.id), str(checked_in.id), str(in_progress.id)} <= ids
    assert not ({str(cancelled.id), str(completed.id), str(no_show.id), str(preempted.id)} & ids)


def test_list_appointments_filters_by_department_id(
    client, db, front_desk_user, staff_password, branch, doctor_record, other_doctor_record, specialty, room, patient
):
    """`doctor_record`/`other_doctor_record` share the same `specialty`
    fixture (see conftest.py) -- give `other_doctor_record` a distinct
    specialty so `department_id` has something real to discriminate on."""
    from app.models.resource import Specialty

    other_specialty = Specialty(name=f"Other-{uuid.uuid4().hex[:8]}")
    db.add(other_specialty)
    db.commit()
    db.refresh(other_specialty)
    other_doctor_record.specialty_id = other_specialty.id
    db.add(other_doctor_record)
    db.commit()

    now = datetime.now(timezone.utc)
    matching = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )
    other = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=other_doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now + timedelta(hours=1),
    )

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(
        "/api/v1/appointments",
        params={"branch_id": str(branch.id), "department_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(matching.id) in ids
    assert str(other.id) not in ids


def test_list_appointments_date_filter(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    today_appt = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )
    tomorrow_appt = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=tomorrow,
    )

    token = _login(client, front_desk_user.email, staff_password)

    default_resp = client.get("/api/v1/appointments", params={"branch_id": str(branch.id)}, headers=_auth(token))
    assert default_resp.status_code == 200, default_resp.text
    default_ids = {row["id"] for row in default_resp.json()}
    assert str(today_appt.id) in default_ids
    assert str(tomorrow_appt.id) not in default_ids

    explicit_resp = client.get(
        "/api/v1/appointments",
        params={"branch_id": str(branch.id), "date": tomorrow.date().isoformat()},
        headers=_auth(token),
    )
    assert explicit_resp.status_code == 200, explicit_resp.text
    explicit_ids = {row["id"] for row in explicit_resp.json()}
    assert str(tomorrow_appt.id) in explicit_ids
    assert str(today_appt.id) not in explicit_ids


def test_list_appointments_422_missing_branch_id(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/appointments", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_appointments_403_pharmacist(client, pharmacist_user, staff_password, branch):
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.get("/api/v1/appointments", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


def test_list_appointments_403_cross_branch(client, front_desk_user, staff_password, other_branch):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/appointments", params={"branch_id": str(other_branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/appointments/available-slots -- HMS Project Completion Prompt
# gap ("available-slot display"). Algorithm coverage lives in
# test_scheduling_engine.py; this is router-level plumbing only.
# ---------------------------------------------------------------------------


def test_available_slots_200(client, db, front_desk_user, staff_password, doctor_record):
    from app.models.resource import DoctorShift

    start = datetime(2030, 3, 1, 9, 0, tzinfo=timezone.utc)
    end = datetime(2030, 3, 1, 10, 0, tzinfo=timezone.utc)
    db.add(DoctorShift(doctor_id=doctor_record.id, shift_range=f"[{start.isoformat()},{end.isoformat()})"))
    db.commit()

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(
        "/api/v1/appointments/available-slots",
        params={"doctor_id": str(doctor_record.id), "date": "2030-03-01", "duration_minutes": 20},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) > 0


def test_available_slots_404_unknown_doctor(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(
        "/api/v1/appointments/available-slots",
        params={"doctor_id": str(uuid.uuid4()), "date": "2030-03-01"},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


def test_available_slots_403_pharmacist(client, pharmacist_user, staff_password, doctor_record):
    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.get(
        "/api/v1/appointments/available-slots",
        params={"doctor_id": str(doctor_record.id), "date": "2030-03-01"},
        headers=_auth(token),
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# PATCH /api/v1/appointments/{id}/reschedule -- HMS Project Completion
# Prompt gap ("rescheduling").
# ---------------------------------------------------------------------------


def test_reschedule_200_books_new_slot_and_cancels_old(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    old = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )
    new_start = now + timedelta(days=1)

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/appointments/{old.id}/reschedule",
        json={
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": new_start.isoformat(),
            "duration_minutes": 30,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "booked"
    assert body["patient_id"] == str(patient.id)
    assert body["reschedule_of_id"] == str(old.id)
    assert body["id"] != str(old.id)

    db.expire_all()
    reloaded_old = db.get(Appointment, old.id)
    assert reloaded_old.status == AppointmentStatus.cancelled


def test_reschedule_409_when_new_slot_conflicts(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    """The new slot is booked FIRST, so a conflict there must leave the OLD
    appointment untouched -- see `scheduling_engine.reschedule_appointment`'s
    docstring."""
    now = datetime.now(timezone.utc)
    old = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )
    conflicting_start = now + timedelta(days=1)
    _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=conflicting_start,
    )

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/appointments/{old.id}/reschedule",
        json={
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": conflicting_start.isoformat(),
            "duration_minutes": 20,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text

    db.expire_all()
    reloaded_old = db.get(Appointment, old.id)
    assert reloaded_old.status == AppointmentStatus.booked  # untouched


def test_reschedule_409_not_reschedulable_status(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    cancelled = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.cancelled,
        start=now,
    )

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/appointments/{cancelled.id}/reschedule",
        json={
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": (now + timedelta(days=1)).isoformat(),
            "duration_minutes": 20,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text


def test_reschedule_404_appointment_not_found(client, front_desk_user, staff_password, doctor_record, room):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/appointments/{uuid.uuid4()}/reschedule",
        json={
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": datetime.now(timezone.utc).isoformat(),
            "duration_minutes": 20,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


def test_reschedule_403_pharmacist(
    client, db, pharmacist_user, staff_password, branch, doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    old = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/appointments/{old.id}/reschedule",
        json={
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": (now + timedelta(days=1)).isoformat(),
            "duration_minutes": 20,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


def test_reschedule_403_cross_branch(
    client, db, front_desk_user, staff_password, other_branch, doctor_record, room, patient
):
    """`old.branch_id` is `other_branch`, but `front_desk_user` belongs to
    `branch` -- `authorize()`'s tenant guard must deny before the
    doctor/room validation even runs, so the FK values here don't need to
    be branch-consistent (this row could never be created through the
    real booking endpoint, only via this direct-insert test helper)."""
    now = datetime.now(timezone.utc)
    old = _make_appointment(
        db,
        branch_id=other_branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        status=AppointmentStatus.booked,
        start=now,
    )

    token = _login(client, front_desk_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/appointments/{old.id}/reschedule",
        json={
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": (now + timedelta(days=1)).isoformat(),
            "duration_minutes": 20,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text
