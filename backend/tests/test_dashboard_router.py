"""Integration tests for the Executive & Operational Dashboard module's HTTP
surface (routers/dashboard.py, services/dashboard_service.py), per
PRPs/executive-dashboard-prp.md Phase 3. Runs against a real Postgres +
Redis (see tests/conftest.py's module docstring), reusing fixtures from
prior modules' TEST-AGENTs (`client`, `db`, `branch`, `other_branch`,
`system_admin_user`, `staff_user` (doctor), `nurse_user`, `front_desk_user`,
`pharmacist_user`, `billing_admin_user`, `lab_tech_user`, `patient`,
`doctor_record`, `room`, `drug`).

Aggregate-correctness of the SQL itself is covered in
test_dashboard_service.py -- this file is about the layer on top: does
`require_role("system_admin")` gate every one of the five endpoints (per the
PRP's design decision #1, there is no non-admin caller anywhere in this
module), is `branch_id` genuinely optional with no 422 the way other
modules' non-admin-facing endpoints require, and do the `days` query
params' defaults (7 for wait-times, 30 for revenue/no-shows) actually take
effect.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import Invoice, InvoiceStatus
from app.models.pharmacy import InventoryBatch, InventoryItem
from app.models.queue import QueueToken, TokenStatus
from app.models.ward import Bed, BedStatus, Ward

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENDPOINTS = (
    "/api/v1/dashboard/occupancy",
    "/api/v1/dashboard/wait-times",
    "/api/v1/dashboard/revenue",
    "/api/v1/dashboard/no-shows",
    "/api/v1/dashboard/stock-alerts",
)


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Role gating -- system_admin only, on ALL five endpoints.
# ---------------------------------------------------------------------------


def test_all_endpoints_200_system_admin(client, system_admin_user, staff_password):
    token = _login(client, system_admin_user.email, staff_password)
    headers = _auth(token)

    for path in _ENDPOINTS:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


@pytest.mark.parametrize(
    "role_fixture",
    [
        "staff_user",  # doctor, see conftest.py's docstring
        "nurse_user",
        "front_desk_user",
        "pharmacist_user",
        "billing_admin_user",
        "lab_tech_user",
    ],
)
def test_all_endpoints_403_for_every_non_system_admin_role(client, request, role_fixture, staff_password):
    """None of doctor/nurse/front_desk/pharmacist/billing_admin/lab_tech
    appear anywhere in the PRP's ENDPOINTS table -- `require_role` must deny
    every single one of these endpoints for every other role in the
    system."""
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)
    headers = _auth(token)

    for path in _ENDPOINTS:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 403, f"{role_fixture} unexpectedly allowed on {path}: {resp.text}"


# ---------------------------------------------------------------------------
# branch_id: genuinely optional (no 422 the way non-admin-facing endpoints
# elsewhere require it), filters correctly when supplied.
# ---------------------------------------------------------------------------


def test_occupancy_branch_id_optional_omitted_aggregates_all_branches(
    client, db, branch, other_branch, system_admin_user, staff_password
):
    ward_a = Ward(branch_id=branch.id, name="Ward A", ward_type="general")
    ward_b = Ward(branch_id=other_branch.id, name="Ward B", ward_type="general")
    db.add_all([ward_a, ward_b])
    db.commit()
    db.refresh(ward_a)
    db.refresh(ward_b)

    token = _login(client, system_admin_user.email, staff_password)

    all_resp = client.get("/api/v1/dashboard/occupancy", headers=_auth(token))
    assert all_resp.status_code == 200, all_resp.text
    all_ward_ids = {row["ward_id"] for row in all_resp.json()}
    assert str(ward_a.id) in all_ward_ids
    assert str(ward_b.id) in all_ward_ids


def test_occupancy_branch_id_supplied_filters_to_that_branch(
    client, db, branch, other_branch, system_admin_user, staff_password
):
    ward_a = Ward(branch_id=branch.id, name="Ward A", ward_type="general")
    ward_b = Ward(branch_id=other_branch.id, name="Ward B", ward_type="general")
    db.add_all([ward_a, ward_b])
    db.commit()
    db.refresh(ward_a)
    db.refresh(ward_b)

    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/dashboard/occupancy", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    ward_ids = {row["ward_id"] for row in resp.json()}
    assert ward_ids == {str(ward_a.id)}


# ---------------------------------------------------------------------------
# days query param defaults: 7 for wait-times, 30 for revenue/no-shows.
# ---------------------------------------------------------------------------


def test_wait_times_days_default_7_and_override(
    client, db, branch, specialty, system_admin_user, staff_password
):
    now = datetime.now(timezone.utc)
    # 10 days ago -- outside the default (7-day) window, inside a wider one.
    token_row = QueueToken(
        branch_id=branch.id,
        department_id=specialty.id,
        token_number=1,
        status=TokenStatus.done,
        checked_in_at=now - timedelta(days=10),
        called_at=now - timedelta(days=10) + timedelta(minutes=15),
    )
    db.add(token_row)
    db.commit()

    token = _login(client, system_admin_user.email, staff_password)

    default_resp = client.get(
        "/api/v1/dashboard/wait-times", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )
    assert default_resp.status_code == 200, default_resp.text
    assert default_resp.json() == []  # default days=7 excludes the 10-day-old token

    wider_resp = client.get(
        "/api/v1/dashboard/wait-times",
        params={"branch_id": str(branch.id), "days": 15},
        headers=_auth(token),
    )
    assert wider_resp.status_code == 200, wider_resp.text
    assert sum(row["sample_size"] for row in wider_resp.json()) == 1


def test_revenue_days_default_30_and_override(
    client, db, branch, patient, system_admin_user, staff_password
):
    now = datetime.now(timezone.utc)
    invoice = Invoice(
        branch_id=branch.id,
        patient_id=patient.id,
        status=InvoiceStatus.paid.value,
        total_amount=Decimal("42.00"),
        created_at=now - timedelta(days=20),
    )
    db.add(invoice)
    db.commit()

    token = _login(client, system_admin_user.email, staff_password)

    default_resp = client.get(
        "/api/v1/dashboard/revenue", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )
    assert default_resp.status_code == 200, default_resp.text
    assert sum(row["invoice_count"] for row in default_resp.json()) == 1  # default days=30 includes it

    narrow_resp = client.get(
        "/api/v1/dashboard/revenue",
        params={"branch_id": str(branch.id), "days": 5},
        headers=_auth(token),
    )
    assert narrow_resp.status_code == 200, narrow_resp.text
    assert sum(row["invoice_count"] for row in narrow_resp.json()) == 0  # excluded by the smaller window


def test_no_shows_days_default_30_and_override(
    client, db, branch, doctor_record, room, patient, system_admin_user, staff_password
):
    start = datetime.now(timezone.utc) - timedelta(days=20)
    end = start + timedelta(minutes=30)
    appt = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.completed,
    )
    db.add(appt)
    db.commit()

    token = _login(client, system_admin_user.email, staff_password)

    default_resp = client.get(
        "/api/v1/dashboard/no-shows", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )
    assert default_resp.status_code == 200, default_resp.text
    assert sum(row["total_considered"] for row in default_resp.json()) == 1  # default days=30 includes it

    narrow_resp = client.get(
        "/api/v1/dashboard/no-shows",
        params={"branch_id": str(branch.id), "days": 5},
        headers=_auth(token),
    )
    assert narrow_resp.status_code == 200, narrow_resp.text
    assert sum(row["total_considered"] for row in narrow_resp.json()) == 0


# ---------------------------------------------------------------------------
# Response shape spot-checks.
# ---------------------------------------------------------------------------


def test_no_shows_response_includes_nonempty_risk_score_note(
    client, db, branch, doctor_record, room, patient, system_admin_user, staff_password
):
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = start + timedelta(minutes=30)
    appt = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.completed,
    )
    db.add(appt)
    db.commit()

    token = _login(client, system_admin_user.email, staff_password)
    resp = client.get(
        "/api/v1/dashboard/no-shows", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    note = body[0]["no_show_risk_score_note"]
    assert isinstance(note, str)
    assert note != ""


def test_stock_alerts_response_has_both_keys_structured_correctly(
    client, db, branch, drug, system_admin_user, staff_password
):
    item = InventoryItem(branch_id=branch.id, drug_id=drug.id, reorder_threshold=50)
    db.add(item)
    db.commit()
    db.refresh(item)
    batch = InventoryBatch(
        inventory_item_id=item.id,
        batch_number="B-EXP",
        quantity=10,
        expiry_date=date.today() + timedelta(days=3),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    token = _login(client, system_admin_user.email, staff_password)
    resp = client.get(
        "/api/v1/dashboard/stock-alerts", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "low_stock" in body
    assert "expiring_soon" in body
    assert isinstance(body["low_stock"], list)
    assert isinstance(body["expiring_soon"], list)

    low_stock_drug_ids = {row["drug_id"] for row in body["low_stock"]}
    expiring_batch_ids = {row["batch_id"] for row in body["expiring_soon"]}
    assert str(drug.id) in low_stock_drug_ids
    assert str(batch.id) in expiring_batch_ids


def test_stock_alerts_empty_when_no_data(client, system_admin_user, staff_password):
    token = _login(client, system_admin_user.email, staff_password)
    resp = client.get("/api/v1/dashboard/stock-alerts", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"low_stock": [], "expiring_soon": []}
