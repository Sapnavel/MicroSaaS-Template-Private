"""Integration tests for the Clinical Consultation & Smart Prescription
Engine module (routers/consultations.py, services/consultation_service.py),
per PRPs/clinical-consultation-prescription-prp.md Phase 3. Runs against a
real Postgres + Redis (see tests/conftest.py's module docstring).

Seeded reference data (database/seed_clinical_reference_data.sql) is applied
once, manually, before this suite runs -- `drugs`/`drug_interactions` are
deliberately excluded from `TRUNCATE_TABLES` (see conftest.py) so they
persist as stable reference data across tests. The exact seeded pairs used
below (confirmed via a live query against the seeded table, not assumed):

    Lanoxin (Digoxin) x Biaxin (Clarithromycin)   -> contraindicated -> BLOCK
    Coumadin (Warfarin) x Bayer Aspirin (Aspirin) -> major           -> OVERRIDE_REQUIRED
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit import AuditLog
from app.models.consultation import Drug, Prescription

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _drug_id(db, name: str) -> str:
    return str(db.execute(select(Drug.id).where(Drug.name == name)).scalar_one())


def _item(drug_id: str, dosage="1 tablet", frequency="twice daily", duration_days=7) -> dict:
    return {"drug_id": drug_id, "dosage": dosage, "frequency": frequency, "duration_days": duration_days}


# ---------------------------------------------------------------------------
# POST /api/v1/consultations
# ---------------------------------------------------------------------------


def test_create_consultation_success_from_in_progress_appointment(
    client, staff_user, staff_password, in_progress_appointment
):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        "/api/v1/consultations",
        json={"appointment_id": str(in_progress_appointment.id), "symptoms": "Cough and fever"},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["appointment_id"] == str(in_progress_appointment.id)
    assert body["doctor_id"] == str(in_progress_appointment.doctor_id)
    assert body["patient_id"] == str(in_progress_appointment.patient_id)
    assert body["symptoms"] == "Cough and fever"
    assert body["ended_at"] is None
    assert body["diagnoses"] == []
    assert body["prescriptions"] == []


def test_create_consultation_400_if_appointment_not_in_progress(
    client, staff_user, staff_password, db, branch, doctor_record, room, patient
):
    token = _login(client, staff_user.email, staff_password)

    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    booked_appointment = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(booked_appointment)
    db.commit()
    db.refresh(booked_appointment)

    resp = client.post(
        "/api/v1/consultations",
        json={"appointment_id": str(booked_appointment.id)},
        headers=_auth(token),
    )
    assert resp.status_code == 400, resp.text


def test_create_consultation_404_unknown_appointment(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        "/api/v1/consultations",
        json={"appointment_id": str(uuid.uuid4())},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


def test_create_consultation_403_when_appointment_belongs_to_a_different_doctor(
    client, other_doctor_user, staff_password, in_progress_appointment
):
    """`in_progress_appointment` belongs to `doctor_record` (== `staff_user`).
    A DIFFERENT doctor (`other_doctor_user`/`other_doctor_record`) trying to
    start a consultation on it must be denied."""
    token = _login(client, other_doctor_user.email, staff_password)

    resp = client.post(
        "/api/v1/consultations",
        json={"appointment_id": str(in_progress_appointment.id)},
        headers=_auth(token),
    )
    assert resp.status_code == 403, resp.text


def test_create_consultation_409_on_second_attempt_same_appointment(
    client, staff_user, staff_password, in_progress_appointment
):
    token = _login(client, staff_user.email, staff_password)
    payload = {"appointment_id": str(in_progress_appointment.id)}

    first = client.post("/api/v1/consultations", json=payload, headers=_auth(token))
    assert first.status_code == 201, first.text

    second = client.post("/api/v1/consultations", json=payload, headers=_auth(token))
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# GET /api/v1/consultations/{id}
# ---------------------------------------------------------------------------


def test_get_consultation_owning_doctor_can_read(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    resp = client.get(f"/api/v1/consultations/{consultation.id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(consultation.id)


def test_get_consultation_different_doctor_gets_403(client, other_doctor_user, staff_password, consultation):
    token = _login(client, other_doctor_user.email, staff_password)

    resp = client.get(f"/api/v1/consultations/{consultation.id}", headers=_auth(token))
    assert resp.status_code == 403, resp.text


def test_get_consultation_nurse_can_read_any(client, nurse_user, staff_password, consultation):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get(f"/api/v1/consultations/{consultation.id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text


def test_get_consultation_system_admin_can_read_any(client, system_admin_user, staff_password, consultation):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(f"/api/v1/consultations/{consultation.id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text


def test_get_consultation_404_unknown_id(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.get(f"/api/v1/consultations/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# PATCH /api/v1/consultations/{id}/complete
# ---------------------------------------------------------------------------


def test_complete_consultation_sets_ended_at_and_accepts_notes(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/consultations/{consultation.id}/complete",
        json={"notes": "Prescribed rest and fluids"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ended_at"] is not None
    assert body["notes"] == "Prescribed rest and fluids"


def test_complete_consultation_409_on_second_attempt(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    first = client.patch(f"/api/v1/consultations/{consultation.id}/complete", json={}, headers=_auth(token))
    assert first.status_code == 200, first.text

    second = client.patch(f"/api/v1/consultations/{consultation.id}/complete", json={}, headers=_auth(token))
    assert second.status_code == 409, second.text


def test_complete_consultation_different_doctor_gets_403(client, other_doctor_user, staff_password, consultation):
    token = _login(client, other_doctor_user.email, staff_password)

    resp = client.patch(f"/api/v1/consultations/{consultation.id}/complete", json={}, headers=_auth(token))
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# POST /api/v1/consultations/{id}/diagnoses
# ---------------------------------------------------------------------------


def test_add_diagnosis_success(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        f"/api/v1/consultations/{consultation.id}/diagnoses",
        json={"icd_code": "J20.9", "description": "Acute bronchitis, unspecified", "is_primary": True},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["icd_code"] == "J20.9"
    assert body["is_primary"] is True
    assert body["consultation_id"] == str(consultation.id)


def test_add_diagnosis_422_on_malformed_icd_code(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        f"/api/v1/consultations/{consultation.id}/diagnoses",
        json={"icd_code": "not-a-code", "description": "Bogus code", "is_primary": False},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


def test_add_diagnosis_400_on_second_is_primary_true(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    first = client.post(
        f"/api/v1/consultations/{consultation.id}/diagnoses",
        json={"icd_code": "J20.9", "description": "Acute bronchitis", "is_primary": True},
        headers=_auth(token),
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"/api/v1/consultations/{consultation.id}/diagnoses",
        json={"icd_code": "R50.9", "description": "Fever, unspecified", "is_primary": True},
        headers=_auth(token),
    )
    assert second.status_code == 400, second.text


# ---------------------------------------------------------------------------
# GET / POST /api/v1/patients/{patient_id}/allergies
# ---------------------------------------------------------------------------


def test_list_and_create_allergy(client, staff_user, staff_password, patient):
    token = _login(client, staff_user.email, staff_password)

    empty = client.get(f"/api/v1/patients/{patient.id}/allergies", headers=_auth(token))
    assert empty.status_code == 200, empty.text
    assert empty.json() == []

    created = client.post(
        f"/api/v1/patients/{patient.id}/allergies",
        json={"substance": "Penicillin", "severity": "severe", "reaction": "Anaphylaxis"},
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["substance"] == "Penicillin"
    assert body["severity"] == "severe"

    listed = client.get(f"/api/v1/patients/{patient.id}/allergies", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1


def test_create_allergy_422_on_invalid_severity(client, staff_user, staff_password, patient):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        f"/api/v1/patients/{patient.id}/allergies",
        json={"substance": "Peanuts", "severity": "extreme"},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


def test_create_allergy_404_unknown_patient(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        f"/api/v1/patients/{uuid.uuid4()}/allergies",
        json={"substance": "Peanuts", "severity": "mild"},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


def test_list_allergies_404_unknown_patient(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.get(f"/api/v1/patients/{uuid.uuid4()}/allergies", headers=_auth(token))
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# POST /api/v1/consultations/{id}/prescriptions -- the safety-gated flow
# ---------------------------------------------------------------------------


def test_create_prescription_block_tier_nothing_persisted_no_override_possible(
    client, staff_user, staff_password, consultation, db
):
    token = _login(client, staff_user.email, staff_password)
    digoxin_id = _drug_id(db, "Lanoxin")
    clarithromycin_id = _drug_id(db, "Biaxin")

    count_before = db.execute(select(func.count()).select_from(Prescription)).scalar_one()

    payload = {"items": [_item(digoxin_id), _item(clarithromycin_id)]}
    resp = client.post(
        f"/api/v1/consultations/{consultation.id}/prescriptions", json=payload, headers=_auth(token)
    )
    assert resp.status_code == 422, resp.text
    assert "blocking_findings" in resp.json()
    assert len(resp.json()["blocking_findings"]) >= 1

    count_after = db.execute(select(func.count()).select_from(Prescription)).scalar_one()
    assert count_after == count_before, "a BLOCK-tier prescription must not persist anything"

    # No way to resubmit with override:true -- still 422, BLOCK cannot be
    # overridden at any input.
    override_payload = {
        "items": [_item(digoxin_id), _item(clarithromycin_id)],
        "override": True,
        "override_reason": "I really want to",
    }
    override_resp = client.post(
        f"/api/v1/consultations/{consultation.id}/prescriptions", json=override_payload, headers=_auth(token)
    )
    assert override_resp.status_code == 422, override_resp.text

    count_final = db.execute(select(func.count()).select_from(Prescription)).scalar_one()
    assert count_final == count_before


def test_create_prescription_override_required_flow_then_succeeds_with_audit_log(
    client, staff_user, staff_password, consultation, db
):
    token = _login(client, staff_user.email, staff_password)
    warfarin_id = _drug_id(db, "Coumadin")
    aspirin_id = _drug_id(db, "Bayer Aspirin")
    payload_items = [_item(warfarin_id), _item(aspirin_id)]
    url = f"/api/v1/consultations/{consultation.id}/prescriptions"

    # 1. override=false (default) -> 409 with override_required_findings.
    first = client.post(url, json={"items": payload_items}, headers=_auth(token))
    assert first.status_code == 409, first.text
    assert "override_required_findings" in first.json()
    assert len(first.json()["override_required_findings"]) >= 1

    # 2. override=true, empty override_reason -> 400.
    second = client.post(
        url,
        json={"items": payload_items, "override": True, "override_reason": ""},
        headers=_auth(token),
    )
    assert second.status_code == 400, second.text

    # 2b. override=true, override_reason missing entirely -> 400.
    third = client.post(url, json={"items": payload_items, "override": True}, headers=_auth(token))
    assert third.status_code == 400, third.text

    audit_count_before = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    # 3. override=true, real reason -> 201, audit log written.
    fourth = client.post(
        url,
        json={"items": payload_items, "override": True, "override_reason": "Benefit outweighs bleeding risk"},
        headers=_auth(token),
    )
    assert fourth.status_code == 201, fourth.text
    prescription_id = fourth.json()["prescription"]["id"]
    assert fourth.json()["prescription"]["status"] == "finalized"

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "prescription.override_safety_warning",
            AuditLog.resource_id == prescription_id,
        )
    ).scalar_one_or_none()
    assert audit_row is not None, "expected an audit_logs row for the override"
    assert audit_row.event_metadata["override_reason"] == "Benefit outweighs bleeding risk"

    audit_count_after = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert audit_count_after == audit_count_before + 1


def test_create_prescription_clean_no_conflicts_201_with_empty_safety_report_no_audit_log(
    client, staff_user, staff_password, consultation, db
):
    token = _login(client, staff_user.email, staff_password)
    metformin_id = _drug_id(db, "Glucophage")

    audit_count_before = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    resp = client.post(
        f"/api/v1/consultations/{consultation.id}/prescriptions",
        json={"items": [_item(metformin_id)]},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["prescription"]["status"] == "finalized"
    assert "safety_report" in body
    assert body["safety_report"]["allergy_findings"] == []
    assert body["safety_report"]["interaction_findings"] == []

    audit_count_after = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert audit_count_after == audit_count_before, "a clean prescription must not create an audit log row"


def test_create_prescription_different_doctor_gets_403(client, other_doctor_user, staff_password, consultation, db):
    token = _login(client, other_doctor_user.email, staff_password)
    metformin_id = _drug_id(db, "Glucophage")

    resp = client.post(
        f"/api/v1/consultations/{consultation.id}/prescriptions",
        json={"items": [_item(metformin_id)]},
        headers=_auth(token),
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/consultations/{id}/prescriptions
# ---------------------------------------------------------------------------


def test_list_prescriptions_returns_created_items_and_is_ownership_gated(
    client, staff_user, staff_password, other_doctor_user, nurse_user, consultation, db
):
    token = _login(client, staff_user.email, staff_password)
    metformin_id = _drug_id(db, "Glucophage")

    create_resp = client.post(
        f"/api/v1/consultations/{consultation.id}/prescriptions",
        json={"items": [_item(metformin_id, dosage="500mg", frequency="once daily", duration_days=30)]},
        headers=_auth(token),
    )
    assert create_resp.status_code == 201, create_resp.text

    owner_list = client.get(f"/api/v1/consultations/{consultation.id}/prescriptions", headers=_auth(token))
    assert owner_list.status_code == 200, owner_list.text
    body = owner_list.json()
    assert len(body) == 1
    assert len(body[0]["items"]) == 1
    assert body[0]["items"][0]["dosage"] == "500mg"

    other_token = _login(client, other_doctor_user.email, staff_password)
    other_list = client.get(
        f"/api/v1/consultations/{consultation.id}/prescriptions", headers=_auth(other_token)
    )
    assert other_list.status_code == 403, other_list.text

    nurse_token = _login(client, nurse_user.email, staff_password)
    nurse_list = client.get(
        f"/api/v1/consultations/{consultation.id}/prescriptions", headers=_auth(nurse_token)
    )
    assert nurse_list.status_code == 200, nurse_list.text
    assert len(nurse_list.json()) == 1
