"""Integration tests for the Billing/Ledger/Insurance-Claims module's HTTP
surface (routers/billing.py, services/billing_service.py), per
PRPs/billing-module-prp.md Phase 3. Runs against a real Postgres + Redis
(see tests/conftest.py's module docstring), reusing fixtures from prior
modules' TEST-AGENTs (`client`, `db`, `branch`, `other_branch`, `patient`,
`doctor_record`, `room`, `staff_user` (doctor), `nurse_user`, `lab_tech_user`,
`pharmacist_user`, `front_desk_user`, `system_admin_user`) plus this module's
own `billing_admin_user`.

Engine-level behavior (lifecycle, state machines, duplicate-charge/
chargeable checks, concurrency) is covered in test_billing_engine.py -- this
file is about the layer on top: does the router map each `billing_engine`
exception to the right status code, does `require_role` gate each endpoint
per the PRP's ENDPOINTS table (including the deliberate `front_desk`/
chargeable-events asymmetry), and does `authorize()`'s tenant guard actually
block cross-branch access to `Invoice`/`InsuranceClaim`.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.appointment import Appointment, AppointmentStatus
from app.models.consultation import Consultation
from app.models.lab import LabOrder, LabOrderStatus
from app.models.patient import Patient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_invoice(client, token, patient_id, branch_id) -> dict:
    resp = client.post(
        "/api/v1/billing/invoices",
        json={"patient_id": str(patient_id), "branch_id": str(branch_id)},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_ended_consultation(db, *, branch, doctor_record, room, patient) -> Consultation:
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    appointment = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)

    consultation = Consultation(
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        patient_id=appointment.patient_id,
        symptoms="Router test",
        ended_at=datetime.now(timezone.utc),
    )
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    return consultation


def _make_lab_order(
    db, *, consultation, patient, ordered_by, status=LabOrderStatus.attached, test_code="CBC"
) -> LabOrder:
    lab_order = LabOrder(
        consultation_id=consultation.id,
        patient_id=patient.id,
        test_code=test_code,
        ordered_by=ordered_by,
        status=status,
    )
    db.add(lab_order)
    db.commit()
    db.refresh(lab_order)
    return lab_order


@pytest.fixture
def other_patient(db) -> Patient:
    p = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Other Billing Router Patient",
        dob=datetime(1992, 5, 1).date(),
        sex="F",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture
def ended_consultation(db, branch, doctor_record, room, patient) -> Consultation:
    return _make_ended_consultation(db, branch=branch, doctor_record=doctor_record, room=room, patient=patient)


# ---------------------------------------------------------------------------
# POST /api/v1/billing/invoices -- role gating + happy path
# ---------------------------------------------------------------------------


def test_create_invoice_201_billing_admin(client, billing_admin_user, staff_password, patient):
    token = _login(client, billing_admin_user.email, staff_password)

    body = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)

    assert body["status"] == "open"
    assert body["total_amount"] == "0.00" or float(body["total_amount"]) == 0.0
    assert body["patient_id"] == str(patient.id)


def test_create_invoice_201_system_admin(client, system_admin_user, staff_password, patient, other_branch):
    """system_admin may create at ANY branch, per the tenant-guard bypass."""
    token = _login(client, system_admin_user.email, staff_password)

    body = _create_invoice(client, token, patient.id, other_branch.id)

    assert body["branch_id"] == str(other_branch.id)


def test_create_invoice_403_front_desk(client, front_desk_user, staff_password, patient):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/billing/invoices",
        json={"patient_id": str(patient.id), "branch_id": str(front_desk_user.branch_id)},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


def test_create_invoice_403_cross_branch_billing_admin(
    client, billing_admin_user, staff_password, patient, other_branch
):
    """billing_admin at `branch` must not create an invoice AT `other_branch`
    -- authorize()'s tenant guard against the `_BranchScoped` stand-in."""
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.post(
        "/api/v1/billing/invoices",
        json={"patient_id": str(patient.id), "branch_id": str(other_branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Role gating: NO access at all for doctor/nurse/lab_tech/pharmacist, on
# EVERY billing endpoint (PRP ENDPOINTS table + item 11).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_fixture", ["staff_user", "nurse_user", "lab_tech_user", "pharmacist_user"]
)
def test_no_other_role_has_any_billing_access(
    client, request, role_fixture, staff_password, billing_admin_user, patient
):
    """`staff_user` is role=doctor (see conftest.py's docstring). None of
    doctor/nurse/lab_tech/pharmacist appear anywhere in the PRP's ENDPOINTS
    table -- `require_role` must deny every single one of these endpoints,
    regardless of whether the referenced invoice/claim even exists."""
    admin_token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, admin_token, patient.id, billing_admin_user.branch_id)
    invoice_id = invoice["id"]

    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)
    headers = _auth(token)

    calls = [
        lambda: client.post(
            "/api/v1/billing/invoices",
            json={"patient_id": str(patient.id), "branch_id": str(billing_admin_user.branch_id)},
            headers=headers,
        ),
        lambda: client.get(f"/api/v1/billing/invoices/{invoice_id}", headers=headers),
        lambda: client.get(f"/api/v1/billing/patients/{patient.id}/invoices", headers=headers),
        lambda: client.get(f"/api/v1/billing/patients/{patient.id}/chargeable-events", headers=headers),
        lambda: client.post(
            f"/api/v1/billing/invoices/{invoice_id}/items",
            json={
                "source_type": "lab_order",
                "source_id": str(uuid.uuid4()),
                "description": "x",
                "amount": "10.00",
            },
            headers=headers,
        ),
        lambda: client.post(
            f"/api/v1/billing/invoices/{invoice_id}/split",
            json={"payer_name": "Acme", "claim_amount": "0.00", "patient_copay": "0.00"},
            headers=headers,
        ),
        lambda: client.patch(
            f"/api/v1/billing/claims/{uuid.uuid4()}/state", json={"state": "adjudicating"}, headers=headers
        ),
        lambda: client.patch(f"/api/v1/billing/invoices/{invoice_id}/mark-paid", headers=headers),
        lambda: client.patch(f"/api/v1/billing/invoices/{invoice_id}/void", headers=headers),
    ]
    for call in calls:
        resp = call()
        assert resp.status_code == 403, f"{role_fixture} unexpectedly allowed: {resp.request.url} -> {resp.text}"


# ---------------------------------------------------------------------------
# front_desk: read-only, EXCEPT chargeable-events (deliberate asymmetry,
# PRP item 11 -- not an oversight).
# ---------------------------------------------------------------------------


def test_front_desk_can_get_invoice(client, billing_admin_user, front_desk_user, staff_password, patient):
    admin_token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, admin_token, patient.id, billing_admin_user.branch_id)

    fd_token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(f"/api/v1/billing/invoices/{invoice['id']}", headers=_auth(fd_token))

    assert resp.status_code == 200, resp.text


def test_front_desk_can_list_patient_invoices(
    client, billing_admin_user, front_desk_user, staff_password, patient
):
    admin_token = _login(client, billing_admin_user.email, staff_password)
    _create_invoice(client, admin_token, patient.id, billing_admin_user.branch_id)

    fd_token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(
        f"/api/v1/billing/patients/{patient.id}/invoices",
        params={"branch_id": str(front_desk_user.branch_id)},
        headers=_auth(fd_token),
    )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) >= 1


def test_front_desk_403_chargeable_events(client, front_desk_user, staff_password, patient):
    """`GET /patients/{id}/chargeable-events` is `billing_admin`/`system_admin`
    ONLY per the PRP's ENDPOINTS table -- front_desk is deliberately NOT on
    that endpoint's auth list, unlike the two plain read endpoints above.
    This is a real, deliberate asymmetry, not a bug to "fix"."""
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        f"/api/v1/billing/patients/{patient.id}/chargeable-events",
        params={"branch_id": str(front_desk_user.branch_id)},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    "method,path_suffix,body",
    [
        ("post", "items", {"source_type": "lab_order", "source_id": str(uuid.uuid4()), "description": "x", "amount": "1.00"}),
        ("post", "split", {"payer_name": "Acme", "claim_amount": "0.00", "patient_copay": "0.00"}),
        ("patch", "mark-paid", None),
        ("patch", "void", None),
    ],
)
def test_front_desk_403_on_every_write_endpoint(
    client, billing_admin_user, front_desk_user, staff_password, patient, method, path_suffix, body
):
    admin_token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, admin_token, patient.id, billing_admin_user.branch_id)

    fd_token = _login(client, front_desk_user.email, staff_password)
    url = f"/api/v1/billing/invoices/{invoice['id']}/{path_suffix}"
    resp = getattr(client, method)(url, json=body, headers=_auth(fd_token))

    assert resp.status_code == 403, resp.text


def test_front_desk_403_create_invoice(client, front_desk_user, staff_password, patient):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/billing/invoices",
        json={"patient_id": str(patient.id), "branch_id": str(front_desk_user.branch_id)},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


def test_front_desk_403_claim_state(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/billing/claims/{uuid.uuid4()}/state", json={"state": "adjudicating"}, headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Status code mapping: 404 / 400 / 409 / 422 via the real HTTP layer.
# ---------------------------------------------------------------------------


def test_get_invoice_404(client, billing_admin_user, staff_password):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.get(f"/api/v1/billing/invoices/{uuid.uuid4()}", headers=_auth(token))

    assert resp.status_code == 404, resp.text


def test_add_item_404_invoice_not_found(client, billing_admin_user, staff_password):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.post(
        f"/api/v1/billing/invoices/{uuid.uuid4()}/items",
        json={"source_type": "lab_order", "source_id": str(uuid.uuid4()), "description": "x", "amount": "10.00"},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


def test_add_item_404_source_event_not_found(client, billing_admin_user, staff_password, patient):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)

    resp = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={"source_type": "lab_order", "source_id": str(uuid.uuid4()), "description": "x", "amount": "10.00"},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


def test_add_item_400_source_event_patient_mismatch(
    client, db, billing_admin_user, staff_password, patient, other_patient, branch, doctor_record, room
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)

    other_consultation = _make_ended_consultation(
        db, branch=branch, doctor_record=doctor_record, room=room, patient=other_patient
    )

    resp = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(other_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 400, resp.text


def test_add_item_409_source_event_not_chargeable(
    client, db, billing_admin_user, staff_password, patient, branch, doctor_record, room
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)

    # Appointment/consultation started but NOT ended -- not chargeable.
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    appointment = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    unfinished_consultation = Consultation(
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        patient_id=appointment.patient_id,
        symptoms="Not yet ended",
    )
    db.add(unfinished_consultation)
    db.commit()
    db.refresh(unfinished_consultation)

    resp = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(unfinished_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text


def test_add_item_409_invalid_invoice_state(
    client, db, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    void_resp = client.patch(f"/api/v1/billing/invoices/{invoice['id']}/void", headers=_auth(token))
    assert void_resp.status_code == 200, void_resp.text

    resp = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text


def test_add_item_409_duplicate_charge(
    client, db, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)

    payload = {
        "source_type": "consultation",
        "source_id": str(ended_consultation.id),
        "description": "Consultation fee",
        "amount": "100.00",
    }
    first = client.post(f"/api/v1/billing/invoices/{invoice['id']}/items", json=payload, headers=_auth(token))
    assert first.status_code == 201, first.text

    second = client.post(f"/api/v1/billing/invoices/{invoice['id']}/items", json=payload, headers=_auth(token))
    assert second.status_code == 409, second.text


def test_split_422_claim_amount_mismatch(
    client, db, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )

    resp = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/split",
        json={"payer_name": "Acme", "claim_amount": "50.00", "patient_copay": "30.00"},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_split_409_invoice_already_split(
    client, db, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )
    first_split = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/split",
        json={"payer_name": "Acme", "claim_amount": "100.00", "patient_copay": "0.00"},
        headers=_auth(token),
    )
    assert first_split.status_code == 200, first_split.text

    second_split = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/split",
        json={"payer_name": "Acme", "claim_amount": "100.00", "patient_copay": "0.00"},
        headers=_auth(token),
    )
    assert second_split.status_code == 409, second_split.text


def test_claim_state_404_claim_not_found(client, billing_admin_user, staff_password):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/billing/claims/{uuid.uuid4()}/state", json={"state": "adjudicating"}, headers=_auth(token)
    )

    assert resp.status_code == 404, resp.text


def test_claim_state_409_illegal_transition(
    client, db, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )
    split_resp = client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/split",
        json={"payer_name": "Acme", "claim_amount": "100.00", "patient_copay": "0.00"},
        headers=_auth(token),
    )
    assert split_resp.status_code == 200, split_resp.text
    claim_id = client.get(f"/api/v1/billing/invoices/{invoice['id']}", headers=_auth(token)).json()["claim"]["id"]

    # submitted -> paid directly, illegal (must go through adjudicating/approved).
    resp = client.patch(
        f"/api/v1/billing/claims/{claim_id}/state", json={"state": "paid"}, headers=_auth(token)
    )

    assert resp.status_code == 409, resp.text


def test_mark_paid_409_invalid_state(client, billing_admin_user, staff_password, patient):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    first = client.patch(f"/api/v1/billing/invoices/{invoice['id']}/mark-paid", headers=_auth(token))
    assert first.status_code == 200, first.text

    second = client.patch(f"/api/v1/billing/invoices/{invoice['id']}/mark-paid", headers=_auth(token))

    assert second.status_code == 409, second.text


def test_void_409_invalid_state(client, billing_admin_user, staff_password, patient):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.patch(f"/api/v1/billing/invoices/{invoice['id']}/mark-paid", headers=_auth(token))

    resp = client.patch(f"/api/v1/billing/invoices/{invoice['id']}/void", headers=_auth(token))

    assert resp.status_code == 409, resp.text


def test_mark_paid_404_invoice_not_found(client, billing_admin_user, staff_password):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.patch(f"/api/v1/billing/invoices/{uuid.uuid4()}/mark-paid", headers=_auth(token))

    assert resp.status_code == 404, resp.text


def test_void_404_invoice_not_found(client, billing_admin_user, staff_password):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.patch(f"/api/v1/billing/invoices/{uuid.uuid4()}/void", headers=_auth(token))

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Chargeable-events discovery -- happy path, and confirms already-billed
# events are excluded.
# ---------------------------------------------------------------------------


def test_chargeable_events_lists_ended_consultation(
    client, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.get(
        f"/api/v1/billing/patients/{patient.id}/chargeable-events",
        params={"branch_id": str(billing_admin_user.branch_id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    source_ids = {row["source_id"] for row in resp.json()}
    assert str(ended_consultation.id) in source_ids


def test_chargeable_events_excludes_already_billed(
    client, billing_admin_user, staff_password, patient, ended_consultation
):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )

    resp = client.get(
        f"/api/v1/billing/patients/{patient.id}/chargeable-events",
        params={"branch_id": str(billing_admin_user.branch_id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    source_ids = {row["source_id"] for row in resp.json()}
    assert str(ended_consultation.id) not in source_ids


def test_list_patient_invoices_422_mismatched_branch(
    client, billing_admin_user, staff_password, patient, other_branch
):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.get(
        f"/api/v1/billing/patients/{patient.id}/invoices",
        params={"branch_id": str(other_branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_chargeable_events_422_mismatched_branch(
    client, billing_admin_user, staff_password, patient, other_branch
):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.get(
        f"/api/v1/billing/patients/{patient.id}/chargeable-events",
        params={"branch_id": str(other_branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Branch tenant isolation: billing_admin/front_desk at Branch A must not
# read or write Branch B's invoice.
# ---------------------------------------------------------------------------


def test_get_invoice_403_cross_branch_billing_admin(
    client, billing_admin_user, system_admin_user, staff_password, patient, other_branch
):
    admin_token = _login(client, system_admin_user.email, staff_password)
    invoice = _create_invoice(client, admin_token, patient.id, other_branch.id)

    ba_token = _login(client, billing_admin_user.email, staff_password)
    resp = client.get(f"/api/v1/billing/invoices/{invoice['id']}", headers=_auth(ba_token))

    assert resp.status_code == 403, resp.text


def test_get_invoice_403_cross_branch_front_desk(
    client, front_desk_user, system_admin_user, staff_password, patient, other_branch
):
    admin_token = _login(client, system_admin_user.email, staff_password)
    invoice = _create_invoice(client, admin_token, patient.id, other_branch.id)

    fd_token = _login(client, front_desk_user.email, staff_password)
    resp = client.get(f"/api/v1/billing/invoices/{invoice['id']}", headers=_auth(fd_token))

    assert resp.status_code == 403, resp.text


def test_get_invoice_200_system_admin_cross_branch(
    client, system_admin_user, staff_password, patient, other_branch
):
    token = _login(client, system_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, other_branch.id)

    resp = client.get(f"/api/v1/billing/invoices/{invoice['id']}", headers=_auth(token))

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "method,path_suffix,body",
    [
        ("post", "items", {"source_type": "lab_order", "source_id": str(uuid.uuid4()), "description": "x", "amount": "1.00"}),
        ("post", "split", {"payer_name": "Acme", "claim_amount": "0.00", "patient_copay": "0.00"}),
        ("patch", "mark-paid", None),
        ("patch", "void", None),
    ],
)
def test_write_endpoints_403_cross_branch_billing_admin(
    client, billing_admin_user, system_admin_user, staff_password, patient, other_branch, method, path_suffix, body
):
    """billing_admin at `branch` must not mutate an invoice that lives at
    `other_branch` -- authorize()'s tenant guard against the real, loaded
    `Invoice.branch_id`."""
    admin_token = _login(client, system_admin_user.email, staff_password)
    invoice = _create_invoice(client, admin_token, patient.id, other_branch.id)

    ba_token = _login(client, billing_admin_user.email, staff_password)
    url = f"/api/v1/billing/invoices/{invoice['id']}/{path_suffix}"
    resp = getattr(client, method)(url, json=body, headers=_auth(ba_token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/billing/claims (frontend UUID-to-dropdown conversion follow-up)
# ---------------------------------------------------------------------------


def _split_invoice(client, token, invoice_id, claim_amount="100.00", patient_copay="0.00") -> dict:
    resp = client.post(
        f"/api/v1/billing/invoices/{invoice_id}/split",
        json={"payer_name": "Acme", "claim_amount": claim_amount, "patient_copay": patient_copay},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_claims_200_happy_path(client, billing_admin_user, staff_password, patient, ended_consultation):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )
    _split_invoice(client, token, invoice["id"])
    claim_id = client.get(f"/api/v1/billing/invoices/{invoice['id']}", headers=_auth(token)).json()["claim"]["id"]

    resp = client.get(
        "/api/v1/billing/claims", params={"branch_id": str(billing_admin_user.branch_id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = next(r for r in body if r["id"] == claim_id)
    assert row["invoice_id"] == invoice["id"]
    assert row["patient_name"] == patient.full_name
    assert row["state"] == "submitted"
    assert float(row["amount"]) == 100.00


def test_list_claims_filters_by_state(client, billing_admin_user, staff_password, patient, ended_consultation):
    token = _login(client, billing_admin_user.email, staff_password)
    invoice = _create_invoice(client, token, patient.id, billing_admin_user.branch_id)
    client.post(
        f"/api/v1/billing/invoices/{invoice['id']}/items",
        json={
            "source_type": "consultation",
            "source_id": str(ended_consultation.id),
            "description": "Consultation fee",
            "amount": "100.00",
        },
        headers=_auth(token),
    )
    _split_invoice(client, token, invoice["id"])

    matching_resp = client.get(
        "/api/v1/billing/claims",
        params={"branch_id": str(billing_admin_user.branch_id), "state": "submitted"},
        headers=_auth(token),
    )
    assert matching_resp.status_code == 200, matching_resp.text
    assert len(matching_resp.json()) == 1

    non_matching_resp = client.get(
        "/api/v1/billing/claims",
        params={"branch_id": str(billing_admin_user.branch_id), "state": "paid"},
        headers=_auth(token),
    )
    assert non_matching_resp.status_code == 200, non_matching_resp.text
    assert non_matching_resp.json() == []


def test_list_claims_422_missing_branch_id(client, billing_admin_user, staff_password):
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.get("/api/v1/billing/claims", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_claims_403_front_desk(client, front_desk_user, staff_password, branch):
    """`GET /billing/claims` is `billing_admin`/`system_admin` ONLY --
    tighter than `_READ_ROLES` (which includes front_desk for plain invoice
    reads), matching ClaimsPage.tsx's route gate."""
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/billing/claims", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


def test_list_claims_422_mismatched_branch(client, billing_admin_user, staff_password, other_branch):
    """`list_claims` resolves `branch_id` through `_resolve_branch_filter`
    (same helper `list_patient_invoices`/`get_chargeable_events` use above),
    which rejects a non-admin's mismatched branch_id with 422, not 403 --
    mirrors `test_list_patient_invoices_422_mismatched_branch` exactly."""
    token = _login(client, billing_admin_user.email, staff_password)

    resp = client.get("/api/v1/billing/claims", params={"branch_id": str(other_branch.id)}, headers=_auth(token))

    assert resp.status_code == 422, resp.text
