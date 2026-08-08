"""Tests for the Patient Self-Service Portal (`/api/v1/me/*`,
routers/patient_portal.py, services/patient_portal_service.py) -- the
previously-entirely-missing "patient role has zero backend access anywhere"
gap. Covers the golden self-service path (create profile -> browse doctors
-> book -> list -> cancel -> rebook) plus the ownership boundary: a second
patient must never see the first patient's appointments/consultations/lab
orders/bills, even though neither resource type carries a `branch_id` the
generic tenant guard could key off (see services/patient_portal_service.py's
module docstring for why ownership is enforced by direct comparison here,
not `core.security.authorize()`).
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import Invoice
from app.models.consultation import Consultation
from app.models.lab import LabOrder
from app.models.patient import Patient


def _login(client, email: str, password: str) -> str:
    """Patient-role accounts authenticate via `/auth/patient/login`, not
    `/auth/login` (staff-only -- see test_auth.py's
    `test_staff_login_rejects_patient_account`, which asserts the reverse
    explicitly)."""
    resp = client.post("/auth/patient/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _staff_login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _profile_payload(**overrides) -> dict:
    payload = {
        "full_name": "Portal Patient",
        "dob": "1992-04-10",
        "sex": "F",
        "phone": f"555-{uuid.uuid4().hex[:7]}",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def test_get_my_patient_404_before_profile_created(client, patient_user, patient_password):
    token = _login(client, patient_user.email, patient_password)
    resp = client.get("/api/v1/me/patient", headers=_auth(token))
    assert resp.status_code == 404, resp.text


def test_create_and_get_my_patient_profile(client, patient_user, patient_password):
    token = _login(client, patient_user.email, patient_password)
    headers = _auth(token)

    create_resp = client.post("/api/v1/me/patient", json=_profile_payload(), headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert body["full_name"] == "Portal Patient"
    assert body["user_id"] == str(patient_user.id)

    get_resp = client.get("/api/v1/me/patient", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == body["id"]


def test_create_my_patient_profile_409_when_already_linked(client, patient_user, patient_password):
    token = _login(client, patient_user.email, patient_password)
    headers = _auth(token)

    first = client.post("/api/v1/me/patient", json=_profile_payload(), headers=headers)
    assert first.status_code == 201, first.text

    second = client.post("/api/v1/me/patient", json=_profile_payload(phone=f"555-{uuid.uuid4().hex[:7]}"), headers=headers)
    assert second.status_code == 409, second.text


def test_staff_role_cannot_reach_patient_portal(client, front_desk_user, staff_password):
    token = _staff_login(client, front_desk_user.email, staff_password)
    resp = client.get("/api/v1/me/patient", headers=_auth(token))
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Doctor search
# ---------------------------------------------------------------------------


def test_search_doctors_unscoped_by_branch(client, patient_user, patient_password, branch, doctor_record):
    token = _login(client, patient_user.email, patient_password)
    resp = client.get("/api/v1/me/doctors", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(doctor_record.id) in ids


# ---------------------------------------------------------------------------
# Available slots -- HMS Project Completion Prompt gap ("available-slot
# display"). Full algorithm coverage lives in test_scheduling_engine.py;
# this is router-level plumbing only.
# ---------------------------------------------------------------------------


def test_available_slots_200_with_shift(client, db, patient_user, patient_password, doctor_record):
    from datetime import datetime, timezone

    from app.models.resource import DoctorShift

    start = datetime(2030, 3, 1, 9, 0, tzinfo=timezone.utc)
    end = datetime(2030, 3, 1, 11, 0, tzinfo=timezone.utc)
    db.add(DoctorShift(doctor_id=doctor_record.id, shift_range=f"[{start.isoformat()},{end.isoformat()})"))
    db.commit()

    token = _login(client, patient_user.email, patient_password)
    resp = client.get(
        f"/api/v1/me/doctors/{doctor_record.id}/available-slots",
        params={"date": "2030-03-01", "duration_minutes": 30},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    slots = resp.json()
    assert len(slots) == 7
    first = datetime.fromisoformat(slots[0].replace("Z", "+00:00"))
    assert first == start


def test_available_slots_404_unknown_doctor(client, patient_user, patient_password):
    token = _login(client, patient_user.email, patient_password)
    resp = client.get(
        f"/api/v1/me/doctors/{uuid.uuid4()}/available-slots",
        params={"date": "2030-03-01"},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Appointments: book / list / cancel / rebook
# ---------------------------------------------------------------------------


def _create_my_profile(client, headers) -> str:
    resp = client.post("/api/v1/me/patient", json=_profile_payload(phone=f"555-{uuid.uuid4().hex[:7]}"), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_book_list_cancel_rebook_my_appointment(
    client, patient_user, patient_password, branch, doctor_record, room
):
    token = _login(client, patient_user.email, patient_password)
    headers = _auth(token)
    _create_my_profile(client, headers)

    start = datetime.now(timezone.utc) + timedelta(days=3)
    payload = {
        "branch_id": str(branch.id),
        "doctor_id": str(doctor_record.id),
        "start_time": start.isoformat(),
        "duration_minutes": 20,
    }

    booked = client.post("/api/v1/me/appointments", json=payload, headers=headers)
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["id"]

    listed = client.get("/api/v1/me/appointments", headers=headers)
    assert listed.status_code == 200, listed.text
    assert appointment_id in {row["id"] for row in listed.json()}

    conflict = client.post("/api/v1/me/appointments", json=payload, headers=headers)
    assert conflict.status_code == 409, conflict.text

    cancelled = client.delete(f"/api/v1/me/appointments/{appointment_id}", headers=headers)
    assert cancelled.status_code == 204, cancelled.text

    rebooked = client.post("/api/v1/me/appointments", json=payload, headers=headers)
    assert rebooked.status_code == 201, rebooked.text


def test_cannot_cancel_another_patients_appointment(
    db, client, patient_user, patient_password, branch, doctor_record, room
):
    token = _login(client, patient_user.email, patient_password)
    headers = _auth(token)
    _create_my_profile(client, headers)

    other_patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Other Portal Patient",
        dob=date(1980, 1, 1),
        sex="M",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(other_patient)
    db.commit()
    db.refresh(other_patient)

    start = datetime.now(timezone.utc) + timedelta(days=4)
    other_appt = Appointment(
        branch_id=branch.id,
        patient_id=other_patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{(start + timedelta(minutes=20)).isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(other_appt)
    db.commit()
    db.refresh(other_appt)

    resp = client.delete(f"/api/v1/me/appointments/{other_appt.id}", headers=headers)
    assert resp.status_code == 404, resp.text

    listed = client.get("/api/v1/me/appointments", headers=headers)
    assert str(other_appt.id) not in {row["id"] for row in listed.json()}


# ---------------------------------------------------------------------------
# Ownership isolation: consultations / lab orders / bills
# ---------------------------------------------------------------------------


def test_cannot_read_another_patients_clinical_or_billing_records(
    db, client, patient_user, patient_password, branch, doctor_record, room
):
    token = _login(client, patient_user.email, patient_password)
    headers = _auth(token)
    _create_my_profile(client, headers)

    other_patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Other Clinical Patient",
        dob=date(1975, 3, 3),
        sex="F",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(other_patient)
    db.commit()
    db.refresh(other_patient)

    start = datetime.now(timezone.utc) - timedelta(hours=1)
    other_appt = Appointment(
        branch_id=branch.id,
        patient_id=other_patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{(start + timedelta(minutes=20)).isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(other_appt)
    db.commit()
    db.refresh(other_appt)

    other_consultation = Consultation(
        appointment_id=other_appt.id,
        doctor_id=doctor_record.id,
        patient_id=other_patient.id,
        symptoms="Unrelated symptoms",
    )
    db.add(other_consultation)
    db.commit()
    db.refresh(other_consultation)

    other_lab_order = LabOrder(
        consultation_id=other_consultation.id,
        patient_id=other_patient.id,
        test_code="CBC",
        ordered_by=doctor_record.user_id,
    )
    db.add(other_lab_order)

    other_invoice = Invoice(patient_id=other_patient.id, branch_id=branch.id)
    db.add(other_invoice)
    db.commit()
    db.refresh(other_lab_order)
    db.refresh(other_invoice)

    consultation_resp = client.get(f"/api/v1/me/consultations/{other_consultation.id}", headers=headers)
    assert consultation_resp.status_code == 404, consultation_resp.text

    bill_resp = client.get(f"/api/v1/me/bills/{other_invoice.id}", headers=headers)
    assert bill_resp.status_code == 404, bill_resp.text

    my_consultations = client.get("/api/v1/me/consultations", headers=headers)
    assert str(other_consultation.id) not in {row["id"] for row in my_consultations.json()}

    my_lab_orders = client.get("/api/v1/me/lab-orders", headers=headers)
    assert str(other_lab_order.id) not in {row["id"] for row in my_lab_orders.json()}

    my_bills = client.get("/api/v1/me/bills", headers=headers)
    assert str(other_invoice.id) not in {row["id"] for row in my_bills.json()}
