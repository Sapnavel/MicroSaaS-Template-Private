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
