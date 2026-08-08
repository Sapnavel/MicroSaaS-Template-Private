"""Integration tests for the Lab module (routers/lab.py, services/lab_service.py,
services/lab_workflow.py), per PRPs/lab-module-prp.md Phase 3. Runs against a
real Postgres + Redis (see tests/conftest.py's module docstring), reusing
fixtures from the Auth, Patient Master Index, and Clinical Consultation
modules' TEST-AGENTs (`db`, `client`, `staff_user`/`doctor_record`,
`other_doctor_user`/`other_doctor_record`, `nurse_user`, `consultation`,
`patient`, `branch`, `room`) plus this module's own `lab_tech_user`.

Validation order in `lab_service.transition_order` (confirmed by reading the
module directly, see its docstring): 404 -> role-per-transition 403 ->
illegal-transition 409 -> sample-already-exists/missing-sample 409 ->
result-validation 422. Tests below are written to distinguish these, not
just assert "some 4xx happened".
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit import AuditLog
from app.models.consultation import Consultation
from app.models.lab import LabSample

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_consultation_for_doctor(db, branch, doctor_record, room, patient) -> Consultation:
    """Same shape as conftest.py's `in_progress_appointment`/`consultation`
    fixtures, but parameterized on an arbitrary doctor -- needed to build a
    SECOND doctor's own consultation for the "doctor's list restricted to
    their own orders" test. Shares the same `room`/`patient` as the primary
    `consultation` fixture: the appointments' overlap-exclusion constraint
    (schema.sql) is keyed on `(doctor_id, time_range)`, not room, so two
    different doctors booked into the same room/time do not conflict."""
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    appt = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    c = Consultation(
        appointment_id=appt.id,
        doctor_id=appt.doctor_id,
        patient_id=appt.patient_id,
        symptoms="Other doctor's patient complaint",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _create_order(client, token, consultation_id, test_code="CBC") -> dict:
    resp = client.post(
        "/api/v1/lab/orders",
        json={"consultation_id": str(consultation_id), "test_code": test_code},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _transition(client, token, order_id, to_status, result=None) -> "object":
    body = {"to_status": to_status}
    if result is not None:
        body["result"] = result
    return client.patch(
        f"/api/v1/lab/orders/{order_id}/transition",
        json=body,
        headers=_auth(token),
    )


def _sample_row(db, order_id) -> LabSample:
    return db.execute(select(LabSample).where(LabSample.lab_order_id == uuid.UUID(order_id))).scalar_one()


def _audit_row(db, order_id, to_status) -> AuditLog:
    return db.execute(
        select(AuditLog).where(
            AuditLog.action == f"lab_order.transitioned_to_{to_status}",
            AuditLog.resource_id == str(order_id),
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# POST /api/v1/lab/orders
# ---------------------------------------------------------------------------


def test_create_order_success(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)

    body = _create_order(client, token, consultation.id, test_code="CBC")

    assert body["patient_id"] == str(consultation.patient_id)
    assert body["ordered_by"] == str(staff_user.id)
    assert body["consultation_id"] == str(consultation.id)
    assert body["test_code"] == "CBC"
    assert body["status"] == "ordered"
    assert body["sample"] is None


def test_create_order_404_unknown_consultation(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.post(
        "/api/v1/lab/orders",
        json={"consultation_id": str(uuid.uuid4()), "test_code": "CBC"},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


def test_create_order_403_when_consultation_belongs_to_a_different_doctor(
    client, other_doctor_user, staff_password, consultation
):
    """`consultation` belongs to `staff_user`/`doctor_record`. A DIFFERENT
    doctor ordering a lab test against it must be denied."""
    token = _login(client, other_doctor_user.email, staff_password)

    resp = client.post(
        "/api/v1/lab/orders",
        json={"consultation_id": str(consultation.id), "test_code": "CBC"},
        headers=_auth(token),
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/lab/orders/{id}
# ---------------------------------------------------------------------------


def test_get_order_owning_doctor_can_read(client, staff_user, staff_password, consultation):
    token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, token, consultation.id)

    resp = client.get(f"/api/v1/lab/orders/{order['id']}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == order["id"]


def test_get_order_different_doctor_gets_403(
    client, staff_user, staff_password, other_doctor_user, consultation
):
    token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, token, consultation.id)

    other_token = _login(client, other_doctor_user.email, staff_password)
    resp = client.get(f"/api/v1/lab/orders/{order['id']}", headers=_auth(other_token))
    assert resp.status_code == 403, resp.text


def test_get_order_nurse_lab_tech_system_admin_can_read_any(
    client, staff_user, staff_password, nurse_user, lab_tech_user, system_admin_user, consultation
):
    token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, token, consultation.id)

    for user, pw in (
        (nurse_user, staff_password),
        (lab_tech_user, staff_password),
        (system_admin_user, staff_password),
    ):
        u_token = _login(client, user.email, pw)
        resp = client.get(f"/api/v1/lab/orders/{order['id']}", headers=_auth(u_token))
        assert resp.status_code == 200, resp.text


def test_get_order_nurse_response_omits_result_key_lab_tech_doctor_admin_see_it(
    client,
    staff_user,
    staff_password,
    nurse_user,
    lab_tech_user,
    system_admin_user,
    consultation,
):
    """Walk a fresh order all the way to `verified` (lab_tech performs every
    step) so a real `result` value exists on the sample, then confirm the
    RAW JSON `nurse` sees has no `result` key inside `sample` at all -- not
    `null` -- while doctor/lab_tech/system_admin's raw JSON does include it
    with the correct decrypted value."""
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)

    lab_tech_token = _login(client, lab_tech_user.email, staff_password)
    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200
    secret_result = "WBC 7.2 x10^9/L"
    verify_resp = _transition(client, lab_tech_token, order["id"], "verified", result=secret_result)
    assert verify_resp.status_code == 200, verify_resp.text

    nurse_token = _login(client, nurse_user.email, staff_password)
    nurse_resp = client.get(f"/api/v1/lab/orders/{order['id']}", headers=_auth(nurse_token))
    assert nurse_resp.status_code == 200, nurse_resp.text
    nurse_body = nurse_resp.json()
    assert "result" not in nurse_body["sample"], (
        "nurse's raw JSON must not contain a 'result' key at all (absence, not null)"
    )

    for user, pw in (
        (staff_user, staff_password),
        (lab_tech_user, staff_password),
        (system_admin_user, staff_password),
    ):
        u_token = _login(client, user.email, pw)
        resp = client.get(f"/api/v1/lab/orders/{order['id']}", headers=_auth(u_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "result" in body["sample"], f"{user.role} must see the 'result' key"
        assert body["sample"]["result"] == secret_result


# ---------------------------------------------------------------------------
# GET /api/v1/lab/orders (list)
# ---------------------------------------------------------------------------


def test_list_orders_422_with_no_filter_params(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.get("/api/v1/lab/orders", headers=_auth(token))
    assert resp.status_code == 422, resp.text


def test_list_orders_filter_by_status_patient_id_consultation_id(
    client, staff_user, staff_password, consultation
):
    token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, token, consultation.id)

    by_status = client.get("/api/v1/lab/orders", params={"status": "ordered"}, headers=_auth(token))
    assert by_status.status_code == 200, by_status.text
    assert any(o["id"] == order["id"] for o in by_status.json())

    by_patient = client.get(
        "/api/v1/lab/orders", params={"patient_id": order["patient_id"]}, headers=_auth(token)
    )
    assert by_patient.status_code == 200, by_patient.text
    assert any(o["id"] == order["id"] for o in by_patient.json())

    by_consultation = client.get(
        "/api/v1/lab/orders", params={"consultation_id": str(consultation.id)}, headers=_auth(token)
    )
    assert by_consultation.status_code == 200, by_consultation.text
    assert any(o["id"] == order["id"] for o in by_consultation.json())

    # A status that doesn't match yields no rows, not an error.
    by_wrong_status = client.get(
        "/api/v1/lab/orders", params={"status": "attached"}, headers=_auth(token)
    )
    assert by_wrong_status.status_code == 200, by_wrong_status.text
    assert not any(o["id"] == order["id"] for o in by_wrong_status.json())


def test_list_orders_doctor_silently_restricted_to_own_consultations(
    client, db, branch, room, patient, staff_user, staff_password, other_doctor_user, other_doctor_record, consultation
):
    """Create one order for `staff_user`'s own consultation and one for
    `other_doctor_user`'s own (separate) consultation; each doctor's list
    must show only their own, even filtering on the same shared status."""
    token = _login(client, staff_user.email, staff_password)
    my_order = _create_order(client, token, consultation.id)

    other_consultation = _make_consultation_for_doctor(db, branch, other_doctor_record, room, patient)
    other_token = _login(client, other_doctor_user.email, staff_password)
    other_order = _create_order(client, other_token, other_consultation.id)

    my_list = client.get("/api/v1/lab/orders", params={"status": "ordered"}, headers=_auth(token))
    assert my_list.status_code == 200, my_list.text
    my_ids = {o["id"] for o in my_list.json()}
    assert my_order["id"] in my_ids
    assert other_order["id"] not in my_ids

    other_list = client.get(
        "/api/v1/lab/orders", params={"status": "ordered"}, headers=_auth(other_token)
    )
    assert other_list.status_code == 200, other_list.text
    other_ids = {o["id"] for o in other_list.json()}
    assert other_order["id"] in other_ids
    assert my_order["id"] not in other_ids


# ---------------------------------------------------------------------------
# PATCH /api/v1/lab/orders/{id}/transition -- full legal chain + side effects
# ---------------------------------------------------------------------------


def test_transition_full_chain_ordered_to_attached_side_effects(
    client, staff_user, staff_password, nurse_user, lab_tech_user, consultation, db
):
    """Walks a fresh order through ordered -> collected -> processing ->
    verified -> attached (nurse collects, lab_tech does the rest -- a
    realistic split per the PRP's "whoever physically draws the sample"
    framing), checking at each step: the right actor+timestamp columns on
    `lab_samples`, `order.status`, and an `audit_logs` row with the right
    `action`/`metadata` (result_submitted is a bool; the raw result value
    never appears anywhere in the audit metadata)."""
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    order_id = order["id"]

    nurse_token = _login(client, nurse_user.email, staff_password)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    # --- ordered -> collected (nurse) ---
    resp = _transition(client, nurse_token, order_id, "collected")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "collected"

    sample = _sample_row(db, order_id)
    assert sample.collected_by == nurse_user.id
    assert sample.collected_at is not None
    assert sample.processed_at is None
    assert sample.verified_by is None

    audit = _audit_row(db, order_id, "collected")
    assert audit.event_metadata["old_status"] == "ordered"
    assert audit.event_metadata["new_status"] == "collected"
    assert audit.event_metadata["actor"] == str(nurse_user.id)
    assert isinstance(audit.event_metadata["result_submitted"], bool)
    assert audit.event_metadata["result_submitted"] is False

    # --- collected -> processing (lab_tech) ---
    resp = _transition(client, lab_tech_token, order_id, "processing")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "processing"

    sample = _sample_row(db, order_id)
    assert sample.processed_at is not None
    assert sample.verified_by is None

    audit = _audit_row(db, order_id, "processing")
    assert audit.event_metadata["actor"] == str(lab_tech_user.id)
    assert audit.event_metadata["result_submitted"] is False

    # --- processing -> verified (lab_tech, with result) ---
    secret_result = "Platelets: 250 x10^9/L"
    resp = _transition(client, lab_tech_token, order_id, "verified", result=secret_result)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "verified"

    sample = _sample_row(db, order_id)
    assert sample.verified_by == lab_tech_user.id
    assert sample.verified_at is not None
    assert sample.result == secret_result  # EncryptedString decrypts transparently on read
    assert sample.attached_to_emr_at is None

    audit = _audit_row(db, order_id, "verified")
    assert audit.event_metadata["result_submitted"] is True
    # PHI must never land in the audit metadata, only a presence flag.
    assert secret_result not in json.dumps(audit.event_metadata)

    # --- verified -> attached (lab_tech, no result) ---
    resp = _transition(client, lab_tech_token, order_id, "attached")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "attached"

    sample = _sample_row(db, order_id)
    assert sample.attached_to_emr_at is not None

    audit = _audit_row(db, order_id, "attached")
    assert audit.event_metadata["result_submitted"] is False


def test_transition_lab_tech_alone_can_perform_all_four_transitions(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "verified", result="all clear").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "attached").status_code == 200


# ---------------------------------------------------------------------------
# PATCH .../transition -- role gating per transition
# ---------------------------------------------------------------------------


def test_transition_nurse_cannot_do_collected_to_processing(
    client, staff_user, staff_password, nurse_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    nurse_token = _login(client, nurse_user.email, staff_password)

    assert _transition(client, nurse_token, order["id"], "collected").status_code == 200

    resp = _transition(client, nurse_token, order["id"], "processing")
    assert resp.status_code == 403, resp.text


def test_transition_nurse_can_do_ordered_to_collected(
    client, staff_user, staff_password, nurse_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    nurse_token = _login(client, nurse_user.email, staff_password)

    resp = _transition(client, nurse_token, order["id"], "collected")
    assert resp.status_code == 200, resp.text


def test_transition_doctor_excluded_entirely_from_transition_endpoint(
    client, staff_user, staff_password, consultation
):
    """doctor never transitions anything -- excluded at the router level
    (`require_role` on the transition endpoint doesn't include "doctor" at
    all), so even a plain legal ordered->collected attempt by the owning
    doctor is a 403, before any state-machine logic runs."""
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)

    resp = _transition(client, doctor_token, order["id"], "collected")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# PATCH .../transition -- illegal transitions (409)
# ---------------------------------------------------------------------------


def test_transition_skip_a_step_ordered_to_processing_409(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    resp = _transition(client, lab_tech_token, order["id"], "processing")
    assert resp.status_code == 409, resp.text


def test_transition_backward_move_409(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200

    # processing -> collected is backward.
    resp = _transition(client, lab_tech_token, order["id"], "collected")
    assert resp.status_code == 409, resp.text


def test_transition_repeat_current_status_409(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200

    # processing -> processing is a repeat of the current status.
    resp = _transition(client, lab_tech_token, order["id"], "processing")
    assert resp.status_code == 409, resp.text


def test_transition_already_attached_order_409(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "verified", result="fine").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "attached").status_code == 200

    resp = _transition(client, lab_tech_token, order["id"], "attached")
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# PATCH .../transition -- result validation (422)
# ---------------------------------------------------------------------------


def test_transition_verified_without_result_422(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200

    resp = _transition(client, lab_tech_token, order["id"], "verified")  # no result
    assert resp.status_code == 422, resp.text


def test_transition_verified_with_result_200_and_decrypts_correctly(
    client, staff_user, staff_password, lab_tech_user, consultation, db
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200

    exact_value = "Glucose: 95 mg/dL (fasting)"
    resp = _transition(client, lab_tech_token, order["id"], "verified", result=exact_value)
    assert resp.status_code == 200, resp.text

    sample = _sample_row(db, order["id"])
    assert sample.result == exact_value


def test_transition_collected_with_result_present_422(
    client, staff_user, staff_password, nurse_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    nurse_token = _login(client, nurse_user.email, staff_password)

    resp = _transition(client, nurse_token, order["id"], "collected", result="sneaky value")
    assert resp.status_code == 422, resp.text


def test_transition_verified_to_attached_with_result_present_422(
    client, staff_user, staff_password, lab_tech_user, consultation
):
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200
    assert _transition(client, lab_tech_token, order["id"], "verified", result="fine").status_code == 200

    resp = _transition(client, lab_tech_token, order["id"], "attached", result="sneaky value")
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# PATCH .../transition -- repeat-collect (409 "sample already exists")
# ---------------------------------------------------------------------------


def test_transition_repeat_collect_second_attempt_is_409(
    client, staff_user, staff_password, nurse_user, consultation, db
):
    """A second `ordered -> collected` attempt for the same order must be
    rejected with 409. Note on WHICH 409 branch actually fires: after the
    first successful collect, `order.status` is already `collected`, so the
    second identical request is caught by `is_legal_transition`'s
    repeat-of-current-status rule (validation step 3) before the explicit
    "sample already exists" check (step 4) is ever reached -- step 4 only
    matters for a hypothetical data-inconsistency case where a sample row
    exists but the order's status wasn't advanced. Either way, exactly one
    `LabSample` row must exist for the order afterward."""
    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    nurse_token = _login(client, nurse_user.email, staff_password)

    first = _transition(client, nurse_token, order["id"], "collected")
    assert first.status_code == 200, first.text

    second = _transition(client, nurse_token, order["id"], "collected")
    assert second.status_code == 409, second.text

    samples = db.execute(
        select(LabSample).where(LabSample.lab_order_id == uuid.UUID(order["id"]))
    ).scalars().all()
    assert len(samples) == 1, "at most one sample per order must be enforced"


def test_verify_transition_publishes_lab_report_ready_event(
    monkeypatch, client, staff_user, staff_password, lab_tech_user, consultation, patient
):
    """HMS Project Completion Prompt gap: `lab.report_ready` had no
    publisher anywhere -- must fire exactly once, at the `verified`
    transition specifically (not `collected`/`processing`/`attached`),
    with `patient_id` matching the order's own patient."""
    from app.services import lab_service

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        lab_service.event_publisher, "publish", lambda topic, payload: published.append((topic, payload))
    )

    doctor_token = _login(client, staff_user.email, staff_password)
    order = _create_order(client, doctor_token, consultation.id)
    lab_tech_token = _login(client, lab_tech_user.email, staff_password)

    assert _transition(client, lab_tech_token, order["id"], "collected").status_code == 200
    assert published == []  # not yet -- only the verify transition fires this

    assert _transition(client, lab_tech_token, order["id"], "processing").status_code == 200
    assert published == []

    verify_resp = _transition(client, lab_tech_token, order["id"], "verified", result="all clear")
    assert verify_resp.status_code == 200, verify_resp.text
    assert published == [("lab.report_ready", {"lab_order_id": order["id"], "patient_id": str(patient.id)})]

    assert _transition(client, lab_tech_token, order["id"], "attached").status_code == 200
    assert len(published) == 1  # still just the one event from the verify step
