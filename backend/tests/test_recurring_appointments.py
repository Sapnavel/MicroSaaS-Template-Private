"""Tests for `POST /api/v1/appointments/recurring`
(routers/appointments.py, services/recurring_appointment_service.py) --
HMS Project Completion Prompt section 3.3, "Repeat or recurring
appointments", confirmed entirely missing by the whole-system audit this
session started from.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.models.appointment import Appointment, AppointmentStatus


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_recurring_appointments_all_occurrences_succeed(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    token = _login(client, front_desk_user.email, staff_password)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor_record.id),
        "room_id": str(room.id),
        "start_time": start.isoformat(),
        "duration_minutes": 20,
        "frequency": "weekly",
        "occurrences": 3,
    }

    resp = client.post("/api/v1/appointments/recurring", json=payload, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["booked"]) == 3
    assert body["failed"] == []
    assert all(a["series_id"] == body["series_id"] for a in body["booked"])
    assert len({a["id"] for a in body["booked"]}) == 3  # three distinct occurrences


def test_recurring_appointments_partial_conflict_reports_both(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    """Occurrence 2 (one week out) is pre-booked by someone else first --
    the series must still return occurrences 1 and 3 as booked, and 2 as a
    reported failure, not fail the whole request."""
    base_start = datetime.now(timezone.utc) + timedelta(days=1)
    conflicting_start = base_start + timedelta(days=7)
    blocker = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{conflicting_start.isoformat()},{(conflicting_start + timedelta(minutes=20)).isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(blocker)
    db.commit()

    token = _login(client, front_desk_user.email, staff_password)
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor_record.id),
        "room_id": str(room.id),
        "start_time": base_start.isoformat(),
        "duration_minutes": 20,
        "frequency": "weekly",
        "occurrences": 3,
    }

    resp = client.post("/api/v1/appointments/recurring", json=payload, headers=_auth(token))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["booked"]) == 2
    assert len(body["failed"]) == 1


def test_recurring_appointments_403_pharmacist(client, pharmacist_user, staff_password, branch, doctor_record, room, patient):
    token = _login(client, pharmacist_user.email, staff_password)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor_record.id),
        "room_id": str(room.id),
        "start_time": start.isoformat(),
        "duration_minutes": 20,
        "frequency": "daily",
        "occurrences": 2,
    }

    resp = client.post("/api/v1/appointments/recurring", json=payload, headers=_auth(token))

    assert resp.status_code == 403, resp.text
