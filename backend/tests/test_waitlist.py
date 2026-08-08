"""Tests for `/api/v1/waitlist` (routers/waitlist.py,
services/waitlist_service.py) -- HMS Project Completion Prompt section 3.3,
"Waiting list" / "Fair slot allocation", confirmed entirely missing by the
whole-system audit this session started from.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

from app.models.appointment import Appointment, AppointmentStatus, WaitlistEntry, WaitlistStatus
from app.models.patient import Patient


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_patient(db, name: str) -> Patient:
    p = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name=name,
        dob=date(1985, 6, 15),
        sex="F",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_join_and_list_waitlist_fifo_order(client, db, front_desk_user, staff_password, branch, doctor_record):
    token = _login(client, front_desk_user.email, staff_password)
    headers = _auth(token)
    requested_date = (date.today() + timedelta(days=3)).isoformat()

    first_patient = _make_patient(db, "First In Line")
    second_patient = _make_patient(db, "Second In Line")

    first_resp = client.post(
        "/api/v1/waitlist",
        json={"patient_id": str(first_patient.id), "doctor_id": str(doctor_record.id), "requested_date": requested_date},
        headers=headers,
    )
    assert first_resp.status_code == 201, first_resp.text
    second_resp = client.post(
        "/api/v1/waitlist",
        json={"patient_id": str(second_patient.id), "doctor_id": str(doctor_record.id), "requested_date": requested_date},
        headers=headers,
    )
    assert second_resp.status_code == 201, second_resp.text

    listed = client.get("/api/v1/waitlist", params={"doctor_id": str(doctor_record.id)}, headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [row["patient_id"] for row in body] == [str(first_patient.id), str(second_patient.id)]


def test_cancelling_appointment_offers_slot_to_oldest_waiting_entry(
    client, db, front_desk_user, staff_password, branch, doctor_record, room, patient
):
    """The fairness algorithm: cancel a booked appointment for a doctor on a
    date two patients are waiting for -- the OLDEST waiting entry (not the
    newest) must be the one flipped to 'offered'."""
    token = _login(client, front_desk_user.email, staff_password)
    headers = _auth(token)

    start = datetime.now(timezone.utc) + timedelta(days=2)
    appt = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{(start + timedelta(minutes=20)).isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    requested_date = start.date().isoformat()
    older_patient = _make_patient(db, "Older Waiter")
    newer_patient = _make_patient(db, "Newer Waiter")

    older_resp = client.post(
        "/api/v1/waitlist",
        json={"patient_id": str(older_patient.id), "doctor_id": str(doctor_record.id), "requested_date": requested_date},
        headers=headers,
    )
    assert older_resp.status_code == 201, older_resp.text
    older_entry_id = older_resp.json()["id"]

    newer_resp = client.post(
        "/api/v1/waitlist",
        json={"patient_id": str(newer_patient.id), "doctor_id": str(doctor_record.id), "requested_date": requested_date},
        headers=headers,
    )
    assert newer_resp.status_code == 201, newer_resp.text
    newer_entry_id = newer_resp.json()["id"]

    cancel_resp = client.delete(f"/api/v1/appointments/{appt.id}", headers=headers)
    assert cancel_resp.status_code == 204, cancel_resp.text

    older = db.get(WaitlistEntry, uuid.UUID(older_entry_id))
    newer = db.get(WaitlistEntry, uuid.UUID(newer_entry_id))
    db.refresh(older)
    db.refresh(newer)

    assert older.status == WaitlistStatus.offered
    assert newer.status == WaitlistStatus.waiting


def test_fulfill_and_cancel_waitlist_entry(client, db, front_desk_user, staff_password, branch, doctor_record, room):
    token = _login(client, front_desk_user.email, staff_password)
    headers = _auth(token)
    requested_date = (date.today() + timedelta(days=5)).isoformat()
    entry_patient = _make_patient(db, "Fulfilled Patient")

    join_resp = client.post(
        "/api/v1/waitlist",
        json={"patient_id": str(entry_patient.id), "doctor_id": str(doctor_record.id), "requested_date": requested_date},
        headers=headers,
    )
    assert join_resp.status_code == 201, join_resp.text
    entry_id = join_resp.json()["id"]

    start = datetime.now(timezone.utc) + timedelta(days=5)
    book_resp = client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(entry_patient.id),
            "doctor_id": str(doctor_record.id),
            "room_id": str(room.id),
            "start_time": start.isoformat(),
            "duration_minutes": 20,
        },
        headers=headers,
    )
    assert book_resp.status_code == 201, book_resp.text
    appointment_id = book_resp.json()["id"]

    fulfill_resp = client.post(
        f"/api/v1/waitlist/{entry_id}/fulfill", json={"appointment_id": appointment_id}, headers=headers
    )
    assert fulfill_resp.status_code == 200, fulfill_resp.text
    assert fulfill_resp.json()["status"] == "fulfilled"

    # A second waitlist entry, cancelled instead of fulfilled.
    second_join = client.post(
        "/api/v1/waitlist",
        json={"patient_id": str(entry_patient.id), "doctor_id": str(doctor_record.id), "requested_date": requested_date},
        headers=headers,
    )
    second_entry_id = second_join.json()["id"]
    cancel_resp = client.delete(f"/api/v1/waitlist/{second_entry_id}", headers=headers)
    assert cancel_resp.status_code == 204, cancel_resp.text
