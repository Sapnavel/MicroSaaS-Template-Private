"""Integration tests for the Patient Master Index module (routers/patients.py,
services/patient_service.py, services/patient_matching.py's DB-backed paths),
per PRPs/patient-master-index-prp.md Phase 3. Runs against a real Postgres +
Redis (see tests/conftest.py's module docstring for the infra rationale).

Two "post-Phase-2 fix" regressions get dedicated tests here, per the Phase 3
brief:
  1. `patient_matching.DUPLICATE_CANDIDATE_SCORE_THRESHOLD` was corrected
     from the PRP draft's 0.5 to 0.25 -- see
     `test_create_patient_dob_name_typo_creates_pending_duplicate_candidate`.
  2. `patient_matching.normalize_phone_for_matching` (digits-only) is wired
     into both create and search so differently-formatted phone numbers
     hash identically -- see
     `test_create_patient_exact_phone_match_different_formatting_returns_409`
     and `test_search_by_phone_uses_same_normalization_as_create`.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit import AuditLog
from app.models.patient import Patient, PatientDuplicateCandidate
from tests.conftest import unique_email

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str, url: str = "/auth/login") -> str:
    resp = client.post(url, json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _patient_payload(**overrides) -> dict:
    payload = {
        "full_name": "Default Patient",
        "dob": "1990-01-01",
        "sex": "F",
        "phone": "555-000-0000",
    }
    payload.update(overrides)
    return payload


def _create_patient(client, token: str, **overrides):
    return client.post("/api/v1/patients", json=_patient_payload(**overrides), headers=_auth(token))


# ---------------------------------------------------------------------------
# Create: role-based response shaping
# ---------------------------------------------------------------------------


def test_create_patient_front_desk_gets_demographics_shape_no_clinical_fields(
    client, front_desk_user, staff_password
):
    token = _login(client, front_desk_user.email, staff_password)

    resp = _create_patient(
        client,
        token,
        full_name="Grace Hopper",
        dob="1975-12-09",
        phone="555-111-2222",
        # front_desk may legitimately submit national_id/address at intake
        # (e.g. transcribing an insurance card), but NOT allergies_note --
        # create_patient() 403s on that combination (REVIEW-AGENT finding
        # M2, see patient_service.py's create_patient), a front_desk-write
        # restriction distinct from the front_desk-read shaping this test
        # is actually about, so allergies_note is deliberately omitted here.
        national_id="NID-001",
        address="123 Compiler Way",
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    for key in ("id", "mrn", "full_name", "dob", "sex", "phone"):
        assert key in body
    for key in ("national_id", "address", "allergies_note"):
        assert key not in body, f"{key} must be ABSENT from front_desk's response, got: {body}"


def test_create_patient_front_desk_cannot_submit_allergies_note(client, front_desk_user, staff_password):
    """REVIEW-AGENT finding M2 (see patient_service.create_patient): front_desk
    can write national_id/address at intake but has no plausible workflow
    for recording clinical allergy history, and can never read it back
    anyway (shape_patient_response strips it) -- submitting it is rejected
    outright rather than silently dropped."""
    token = _login(client, front_desk_user.email, staff_password)

    resp = _create_patient(
        client,
        token,
        full_name="Should Be Rejected",
        dob="1999-09-09",
        phone="555-999-1234",
        allergies_note="Peanuts",
    )
    assert resp.status_code == 403, resp.text


def test_create_patient_doctor_gets_full_record_shape_with_clinical_fields(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = _create_patient(
        client,
        token,
        full_name="Ada Lovelace",
        dob="1980-03-03",
        phone="555-222-3333",
        national_id="NID-002",
        address="1 Analytical Engine Rd",
        allergies_note="None known",
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    for key in ("national_id", "address", "allergies_note"):
        assert key in body, f"{key} must be PRESENT in doctor's full-record response, got: {body}"
    assert body["national_id"] == "NID-002"
    assert body["allergies_note"] == "None known"


def test_create_patient_nurse_gets_full_record_shape(client, nurse_user, staff_password):
    token = _login(client, nurse_user.email, staff_password)

    resp = _create_patient(client, token, full_name="Florence N", dob="1970-05-12", phone="555-333-4444")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "allergies_note" in body


def test_create_patient_system_admin_gets_full_record_shape(client, system_admin_user, staff_password):
    token = _login(client, system_admin_user.email, staff_password)

    resp = _create_patient(client, token, full_name="Admin Person", dob="1965-01-01", phone="555-444-5555")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "allergies_note" in body


# ---------------------------------------------------------------------------
# Deterministic dedup: exact phone match despite different formatting (fix #2)
# ---------------------------------------------------------------------------


def test_create_patient_exact_phone_match_different_formatting_returns_409(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    first = _create_patient(
        client, token, full_name="Patient One", dob="1975-05-05", phone="5551234567"
    )
    assert first.status_code == 201, first.text
    first_body = first.json()

    second = _create_patient(
        # Same phone number, different formatting -- must still be detected
        # as an exact match via normalize_phone_for_matching + deterministic_hash.
        client,
        token,
        full_name="Patient Two Different Name",
        dob="1990-01-01",
        phone="555-123-4567",
    )

    assert second.status_code == 409, second.text
    body = second.json()
    # Top-level keys, NOT nested under "detail" -- routers/patients.py builds
    # this by hand for exactly this reason (a plain HTTPException(detail=...)
    # can't produce top-level keys alongside detail).
    assert body["existing_patient_id"] == first_body["id"]
    assert body["existing_patient_mrn"] == first_body["mrn"]
    assert "detail" in body


def test_create_patient_force_true_bypasses_conflict_and_is_audit_logged(client, front_desk_user, staff_password, db):
    token = _login(client, front_desk_user.email, staff_password)

    first = _create_patient(client, token, full_name="Force One", dob="1975-05-05", phone="5559876543")
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = _create_patient(
        client,
        token,
        full_name="Force Two Unrelated Name",
        dob="1990-01-01",
        phone="555-987-6543",  # same number, different formatting
        force=True,
    )
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["id"] != first_id

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "patient.force_create_despite_match",
            AuditLog.resource_id == second_body["id"],
        )
    ).scalar_one_or_none()
    assert audit_row is not None, "expected an audit_logs row for the force-create override"
    assert audit_row.event_metadata["existing_patient_id"] == first_id


# ---------------------------------------------------------------------------
# Probabilistic dedup: DOB + name-typo, different phone (fix #1 regression)
# ---------------------------------------------------------------------------


def test_create_patient_dob_name_typo_creates_pending_duplicate_candidate(client, front_desk_user, staff_password, db):
    """Regression test for the threshold fix: DUPLICATE_CANDIDATE_SCORE_THRESHOLD
    was corrected from the PRP draft's 0.5 to 0.25 because 0.5 was
    mathematically unreachable for exactly this scenario (same DOB, a
    plausible name typo, no phone/national_id match). Confirmed against the
    real Postgres pg_trgm similarity() for this pair: 'Jonathan Smith' vs
    'Jonathon Smith' == 0.6666667 (measured directly via
    `select similarity('Jonathan Smith','Jonathon Smith')`), which is within
    the 0.5-0.8 "plausible typo" range the task asked for, and yields a
    total score of 0.15 (dob) + 0.6667*0.25 (name) ~= 0.3167 -- above 0.25,
    below the old (wrong) 0.5 floor."""
    token = _login(client, front_desk_user.email, staff_password)

    first = _create_patient(
        client, token, full_name="Jonathan Smith", dob="1985-06-15", phone="555-100-1000"
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = _create_patient(
        client, token, full_name="Jonathon Smith", dob="1985-06-15", phone="555-200-2000"
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]

    pair_ids = {first_id, second_id}
    candidate = db.execute(select(PatientDuplicateCandidate)).scalars().one()
    assert {str(candidate.patient_a_id), str(candidate.patient_b_id)} == pair_ids
    assert candidate.status == "pending"
    assert candidate.match_score >= 0.25
    assert candidate.match_score < 0.5, (
        "score must stay below the PRP draft's mathematically-unreachable 0.5 floor "
        f"for this scenario -- got {candidate.match_score}"
    )
    assert "dob" in candidate.match_reason
    assert "full_name" in candidate.match_reason
    assert "phone_hash" not in candidate.match_reason


# ---------------------------------------------------------------------------
# GET /api/v1/patients/duplicates
# ---------------------------------------------------------------------------


def test_list_duplicates_shows_pending_and_excludes_rejected(client, front_desk_user, staff_password, db):
    token = _login(client, front_desk_user.email, staff_password)

    first = _create_patient(client, token, full_name="Typo Alpha", dob="1988-02-02", phone="555-500-5000")
    second = _create_patient(client, token, full_name="Typo Alfa", dob="1988-02-02", phone="555-600-6000")
    assert first.status_code == 201 and second.status_code == 201

    candidate = db.execute(select(PatientDuplicateCandidate)).scalars().one()

    list_resp = client.get("/api/v1/patients/duplicates", headers=_auth(token))
    assert list_resp.status_code == 200, list_resp.text
    ids_before = {row["id"] for row in list_resp.json()}
    assert str(candidate.id) in ids_before

    reject_resp = client.post(
        f"/api/v1/patients/duplicates/{candidate.id}/reject", headers=_auth(token)
    )
    assert reject_resp.status_code == 204, reject_resp.text

    list_resp_after = client.get("/api/v1/patients/duplicates", headers=_auth(token))
    ids_after = {row["id"] for row in list_resp_after.json()}
    assert str(candidate.id) not in ids_after


# ---------------------------------------------------------------------------
# GET /api/v1/patients/search
# ---------------------------------------------------------------------------


def test_search_with_no_params_returns_422(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/patients/search", headers=_auth(token))
    assert resp.status_code == 422


def test_search_by_phone_uses_same_normalization_as_create(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    created = _create_patient(
        client, token, full_name="Searchable Sam", dob="1992-04-04", phone="5551234567"
    )
    assert created.status_code == 201, created.text
    created_id = created.json()["id"]

    resp = client.get(
        "/api/v1/patients/search", params={"phone": "555-123-4567"}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert any(row["id"] == created_id for row in results), results


def test_search_billing_admin_gets_demographics_shape(client, front_desk_user, billing_admin_user, staff_password):
    """HMS Project Completion Prompt gap: `billing_admin` needs patient
    search to use the Invoice page's patient picker
    (`PatientSearchSelect.tsx`), narrower than the full clinical-role read
    -- demographics-only, same shape as `front_desk`, not the full record
    doctor/nurse get."""
    front_desk_token = _login(client, front_desk_user.email, staff_password)
    created = _create_patient(
        client, front_desk_token, full_name="Billing Search Target", dob="1988-08-08", phone="555-321-9876"
    )
    assert created.status_code == 201, created.text
    created_id = created.json()["id"]

    billing_token = _login(client, billing_admin_user.email, staff_password)
    resp = client.get(
        "/api/v1/patients/search", params={"phone": "555-321-9876"}, headers=_auth(billing_token)
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()
    matches = [row for row in results if row["id"] == created_id]
    assert len(matches) == 1, results
    for key in ("national_id", "address", "allergies_note"):
        assert key not in matches[0], f"{key} must be ABSENT from billing_admin's response, got: {matches[0]}"


def test_get_patient_billing_admin_gets_demographics_shape(
    client, front_desk_user, billing_admin_user, staff_password
):
    front_desk_token = _login(client, front_desk_user.email, staff_password)
    created = _create_patient(
        client, front_desk_token, full_name="Billing Get Target", dob="1991-01-01", phone="555-654-3210"
    )
    assert created.status_code == 201, created.text
    created_id = created.json()["id"]

    billing_token = _login(client, billing_admin_user.email, staff_password)
    resp = client.get(f"/api/v1/patients/{created_id}", headers=_auth(billing_token))

    assert resp.status_code == 200, resp.text
    assert "allergies_note" not in resp.json()


def test_create_patient_billing_admin_403(client, billing_admin_user, staff_password):
    """`billing_admin` can look patients up (see the two tests above) but
    has no business creating them -- that stays front_desk/doctor/nurse/
    system_admin only."""
    token = _login(client, billing_admin_user.email, staff_password)

    resp = _create_patient(client, token, full_name="Should Be Rejected", dob="1990-01-01", phone="555-000-1111")

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Merge workflow
# ---------------------------------------------------------------------------


def _make_duplicate_pair(client, token, *, name_a="Merge Case Anna", name_b="Merge Case Ana", dob="1979-09-09"):
    resp_a = _create_patient(client, token, full_name=name_a, dob=dob, phone="555-700-7000")
    resp_b = _create_patient(client, token, full_name=name_b, dob=dob, phone="555-800-8000")
    assert resp_a.status_code == 201, resp_a.text
    assert resp_b.status_code == 201, resp_b.text
    return resp_a.json(), resp_b.json()


def test_merge_reassigns_appointments_and_updates_all_records(
    client, front_desk_user, staff_password, db, branch, doctor_record, room
):
    token = _login(client, front_desk_user.email, staff_password)
    patient_a, patient_b = _make_duplicate_pair(client, token)

    candidate = db.execute(select(PatientDuplicateCandidate)).scalars().one()

    # survivor is arbitrarily patient_a; loser is whichever of the pair
    # isn't the survivor -- patient_service._ordered_pair may have stored
    # a/b in either order, so this is computed rather than assumed.
    survivor_id = patient_a["id"]
    loser_id = patient_b["id"]

    start = datetime.now(timezone.utc) + timedelta(days=1)
    end = start + timedelta(minutes=30)
    appointment = Appointment(
        branch_id=branch.id,
        patient_id=uuid.UUID(loser_id),
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)

    audit_count_before = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    merge_resp = client.post(
        f"/api/v1/patients/duplicates/{candidate.id}/merge",
        json={"survivor_id": survivor_id},
        headers=_auth(token),
    )
    assert merge_resp.status_code == 200, merge_resp.text
    merge_body = merge_resp.json()
    assert merge_body["reassigned_appointments"] == 1
    assert merge_body["survivor"]["id"] == survivor_id

    # Re-query everything fresh from the DB rather than trusting in-session
    # identity-map objects, since the merge happened inside the request's
    # own transaction/commit.
    reloaded_appointment = db.execute(
        select(Appointment).where(Appointment.id == appointment.id)
    ).scalar_one()
    assert str(reloaded_appointment.patient_id) == survivor_id

    reloaded_loser = db.get(Patient, uuid.UUID(loser_id))
    assert str(reloaded_loser.merged_into_id) == survivor_id

    reloaded_candidate = db.get(PatientDuplicateCandidate, candidate.id)
    assert reloaded_candidate.status == "confirmed_merge"
    assert reloaded_candidate.reviewed_by == front_desk_user.id

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "patient.merged",
            AuditLog.resource_id == survivor_id,
        )
    ).scalar_one_or_none()
    assert audit_row is not None
    assert audit_row.event_metadata["loser_id"] == loser_id

    audit_count_after = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert audit_count_after == audit_count_before + 1

    # Bonus: the now-merged candidate must drop out of the pending queue.
    list_resp = client.get("/api/v1/patients/duplicates", headers=_auth(token))
    ids = {row["id"] for row in list_resp.json()}
    assert str(candidate.id) not in ids


def test_merge_survivor_id_not_in_candidate_pair_returns_400(client, front_desk_user, staff_password, db):
    token = _login(client, front_desk_user.email, staff_password)
    patient_a, patient_b = _make_duplicate_pair(client, token)
    candidate = db.execute(select(PatientDuplicateCandidate)).scalars().one()

    unrelated = _create_patient(
        client, token, full_name="Totally Unrelated Person", dob="2001-01-01", phone="555-900-9000"
    )
    assert unrelated.status_code == 201, unrelated.text
    unrelated_id = unrelated.json()["id"]

    resp = client.post(
        f"/api/v1/patients/duplicates/{candidate.id}/merge",
        json={"survivor_id": unrelated_id},
        headers=_auth(token),
    )
    assert resp.status_code == 400


def test_merge_rejects_when_a_patient_is_already_merged(client, front_desk_user, staff_password, db):
    token = _login(client, front_desk_user.email, staff_password)
    patient_a, patient_b = _make_duplicate_pair(client, token)
    candidate = db.execute(select(PatientDuplicateCandidate)).scalars().one()

    first_merge = client.post(
        f"/api/v1/patients/duplicates/{candidate.id}/merge",
        json={"survivor_id": patient_a["id"]},
        headers=_auth(token),
    )
    assert first_merge.status_code == 200, first_merge.text

    # Same candidate, same pair -- the loser now has merged_into_id set, so
    # a second merge attempt against this same pair must be rejected.
    second_merge = client.post(
        f"/api/v1/patients/duplicates/{candidate.id}/merge",
        json={"survivor_id": patient_a["id"]},
        headers=_auth(token),
    )
    assert second_merge.status_code == 400


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def test_reject_marks_status_and_does_not_audit_log(client, front_desk_user, staff_password, db):
    token = _login(client, front_desk_user.email, staff_password)
    patient_a, patient_b = _make_duplicate_pair(client, token)
    candidate = db.execute(select(PatientDuplicateCandidate)).scalars().one()

    audit_count_before = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    resp = client.post(f"/api/v1/patients/duplicates/{candidate.id}/reject", headers=_auth(token))
    assert resp.status_code == 204, resp.text

    reloaded = db.get(PatientDuplicateCandidate, candidate.id)
    assert reloaded.status == "rejected"
    assert reloaded.reviewed_by == front_desk_user.id

    audit_count_after = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert audit_count_after == audit_count_before, "reject must not create an audit_logs row"


# ---------------------------------------------------------------------------
# Role gating: `patient` role has no registered require_role entry for any
# of these endpoints (_READ_ROLES / _REVIEW_ROLES are both staff-only), so
# every one of them must 403 before any business logic runs.
# ---------------------------------------------------------------------------


def test_patient_role_is_forbidden_from_every_patient_module_endpoint(client, patient_user, patient_password):
    token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")
    random_id = str(uuid.uuid4())

    create_resp = _create_patient(client, token, full_name="Should Not Be Created", dob="2000-01-01", phone="555-000-1111")
    assert create_resp.status_code == 403

    search_resp = client.get("/api/v1/patients/search", params={"phone": "555-000-1111"}, headers=_auth(token))
    assert search_resp.status_code == 403

    get_resp = client.get(f"/api/v1/patients/{random_id}", headers=_auth(token))
    assert get_resp.status_code == 403

    duplicates_resp = client.get("/api/v1/patients/duplicates", headers=_auth(token))
    assert duplicates_resp.status_code == 403

    merge_resp = client.post(
        f"/api/v1/patients/duplicates/{random_id}/merge",
        json={"survivor_id": random_id},
        headers=_auth(token),
    )
    assert merge_resp.status_code == 403

    reject_resp = client.post(f"/api/v1/patients/duplicates/{random_id}/reject", headers=_auth(token))
    assert reject_resp.status_code == 403
