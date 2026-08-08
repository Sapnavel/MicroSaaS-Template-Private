"""Tests for the unified Patient Timeline (HMS Project Completion Prompt
gap: "Patient medical timeline"). Covers both entry points --
`GET /api/v1/patients/{id}/timeline` (staff, routers/patients.py) and
`GET /api/v1/me/timeline` (patient, routers/patient_portal.py) -- since both
are thin wrappers around the same `patient_timeline_service.get_patient_timeline`,
whose merge/sort logic is exercised once here at the router level rather
than duplicated as a separate service-level test file.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import Invoice
from app.models.consultation import Consultation, Prescription
from app.models.lab import LabOrder, LabOrderStatus
from app.models.ward import Admission


def _login(client, email: str, password: str, url: str = "/auth/login") -> str:
    resp = client.post(url, json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_full_history(db, *, branch, doctor_record, room, bed, patient, actor_id):
    now = datetime.now(timezone.utc)

    appointment = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{(now - timedelta(days=10)).isoformat()},{(now - timedelta(days=10) + timedelta(minutes=20)).isoformat()})",
        status=AppointmentStatus.completed,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)

    consultation = Consultation(
        appointment_id=appointment.id,
        doctor_id=doctor_record.id,
        patient_id=patient.id,
        symptoms="Headache",
        started_at=now - timedelta(days=9),
    )
    db.add(consultation)
    db.commit()
    db.refresh(consultation)

    lab_order = LabOrder(
        consultation_id=consultation.id,
        patient_id=patient.id,
        test_code="CBC",
        ordered_by=actor_id,
        status=LabOrderStatus.ordered,
    )
    db.add(lab_order)
    db.commit()
    db.refresh(lab_order)
    lab_order.created_at = now - timedelta(days=8)
    db.add(lab_order)
    db.commit()

    prescription = Prescription(consultation_id=consultation.id, patient_id=patient.id, status="finalized")
    db.add(prescription)
    db.commit()
    db.refresh(prescription)
    prescription.created_at = now - timedelta(days=7)
    db.add(prescription)
    db.commit()

    admission_start = now - timedelta(days=6)
    admission = Admission(
        patient_id=patient.id,
        bed_id=bed.id,
        stay_range=f"[{admission_start.isoformat()},)",
        admitted_by=actor_id,
    )
    db.add(admission)
    db.commit()
    db.refresh(admission)

    invoice = Invoice(patient_id=patient.id, branch_id=branch.id, status="open", total_amount=Decimal("50.00"))
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    return {
        "appointment": appointment,
        "consultation": consultation,
        "lab_order": lab_order,
        "prescription": prescription,
        "admission": admission,
        "invoice": invoice,
    }


def test_staff_timeline_200_includes_every_event_type_sorted_most_recent_first(
    client, db, staff_user, staff_password, branch, doctor_record, room, bed, patient
):
    rows = _seed_full_history(
        db, branch=branch, doctor_record=doctor_record, room=room, bed=bed, patient=patient, actor_id=staff_user.id
    )

    token = _login(client, staff_user.email, staff_password)
    resp = client.get(f"/api/v1/patients/{patient.id}/timeline", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    events = resp.json()
    types = {e["event_type"] for e in events}
    assert types == {"appointment", "consultation", "lab_order", "prescription", "admission", "invoice"}

    occurred_ats = [datetime.fromisoformat(e["occurred_at"].replace("Z", "+00:00")) for e in events]
    assert occurred_ats == sorted(occurred_ats, reverse=True)

    # invoice (just now) should sort before the appointment (10 days ago).
    invoice_index = next(i for i, e in enumerate(events) if e["event_type"] == "invoice")
    appointment_index = next(i for i, e in enumerate(events) if e["event_type"] == "appointment")
    assert invoice_index < appointment_index


def test_staff_timeline_403_front_desk(client, front_desk_user, staff_password, patient):
    """`front_desk` is deliberately excluded -- see
    services/patient_timeline_service.py's module docstring."""
    token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(f"/api/v1/patients/{patient.id}/timeline", headers=_auth(token))
    assert resp.status_code == 403, resp.text


def test_staff_timeline_403_billing_admin(client, billing_admin_user, staff_password, patient):
    token = _login(client, billing_admin_user.email, staff_password)
    resp = client.get(f"/api/v1/patients/{patient.id}/timeline", headers=_auth(token))
    assert resp.status_code == 403, resp.text


def test_staff_timeline_200_nurse(client, nurse_user, staff_password, patient):
    token = _login(client, nurse_user.email, staff_password)
    resp = client.get(f"/api/v1/patients/{patient.id}/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text


def test_staff_timeline_404_unknown_patient(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)
    resp = client.get(f"/api/v1/patients/{uuid.uuid4()}/timeline", headers=_auth(token))
    assert resp.status_code == 404, resp.text


def test_my_timeline_200_sees_own_events(client, db, patient_user, patient_password, branch, doctor_record, room, bed, staff_user):
    """Patient-facing `/me/timeline` derives `patient_id` from the caller's
    own linked `Patient` row -- create one via the real registration
    endpoint first, matching every other `/me/*` test's setup."""
    from app.models.patient import Patient
    from sqlalchemy import select

    linked_patient = db.execute(select(Patient).where(Patient.user_id == patient_user.id)).scalar_one_or_none()
    if linked_patient is None:
        token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")
        resp = client.post(
            "/api/v1/me/patient",
            json={
                "full_name": "Timeline Test Patient",
                "dob": "1990-01-01",
                "sex": "F",
                "phone": f"555-{uuid.uuid4().hex[:7]}",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        linked_patient = db.execute(select(Patient).where(Patient.user_id == patient_user.id)).scalar_one()

    _seed_full_history(
        db,
        branch=branch,
        doctor_record=doctor_record,
        room=room,
        bed=bed,
        patient=linked_patient,
        actor_id=staff_user.id,
    )

    token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")
    resp = client.get("/api/v1/me/timeline", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    types = {e["event_type"] for e in resp.json()}
    assert types == {"appointment", "consultation", "lab_order", "prescription", "admission", "invoice"}
