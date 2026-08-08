"""Integration tests for the generic reference-data lookup endpoints
(routers/directory.py, services/directory_service.py) -- the frontend
UUID-to-dropdown conversion follow-up's backend phase. These are read-only,
low-risk endpoints; per the task brief, a happy path + one auth-denial case
per endpoint is enough, not exhaustive coverage (see the existing
test_queue_router.py / test_wards_router.py for the fuller-coverage style
this file deliberately does not attempt to match).
"""

import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str, url: str = "/auth/login") -> str:
    resp = client.post(url, json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /api/v1/branches
# ---------------------------------------------------------------------------


def test_list_branches_200_any_staff_role(client, front_desk_user, staff_password, branch):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/branches", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(branch.id) in ids
    assert resp.json()[0]["name"]


def test_list_branches_200_patient(client, patient_user, patient_password, branch):
    """`patient` is deliberately allowed here (unlike every other endpoint
    in this router) -- see routers/directory.py's `_ANY_AUTHENTICATED_ROLE`:
    a patient self-booking via `POST /api/v1/me/appointments` needs to pick
    a branch first, and branch names carry no sensitivity."""
    token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")

    resp = client.get("/api/v1/branches", headers=_auth(token))

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/specialties
# ---------------------------------------------------------------------------


def test_list_specialties_200_any_staff_role(client, nurse_user, staff_password, specialty):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/specialties", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert specialty.id in ids


def test_list_specialties_200_patient(client, patient_user, patient_password, specialty):
    """Same reasoning as `test_list_branches_200_patient` above -- a patient
    needs to narrow doctor search by specialty before booking."""
    token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")

    resp = client.get("/api/v1/specialties", headers=_auth(token))

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/doctors
# ---------------------------------------------------------------------------


def test_list_doctors_200_own_branch(client, front_desk_user, staff_password, doctor_record, staff_user, specialty):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/doctors", params={"branch_id": str(front_desk_user.branch_id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(doctor_record.id)
    assert body[0]["full_name"] == staff_user.full_name
    assert body[0]["specialty_id"] == specialty.id
    assert body[0]["branch_id"] == str(front_desk_user.branch_id)


def test_list_doctors_filters_by_specialty_id(
    client, front_desk_user, staff_password, doctor_record, other_doctor_record, specialty, db
):
    """`other_doctor_record` shares the same `specialty` fixture as
    `doctor_record` (see conftest.py) -- add a second specialty so the
    filter has something real to discriminate on."""
    from app.models.resource import Specialty

    other_specialty = Specialty(name=f"Other-{uuid.uuid4().hex[:8]}")
    db.add(other_specialty)
    db.commit()
    db.refresh(other_specialty)
    doctor_record.specialty_id = other_specialty.id
    db.add(doctor_record)
    db.commit()

    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/doctors",
        params={"branch_id": str(front_desk_user.branch_id), "specialty_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(other_doctor_record.id) in ids
    assert str(doctor_record.id) not in ids


def test_list_doctors_422_missing_branch_id(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/doctors", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_doctors_403_cross_branch(client, front_desk_user, staff_password, other_branch):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/doctors", params={"branch_id": str(other_branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


def test_list_doctors_403_patient(client, patient_user, patient_password, branch):
    token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")

    resp = client.get("/api/v1/doctors", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/rooms
# ---------------------------------------------------------------------------


def test_list_rooms_200_own_branch(client, nurse_user, staff_password, room):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/rooms", params={"branch_id": str(nurse_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(room.id)
    assert body[0]["room_type"] == "consultation"


def test_list_rooms_filters_by_room_type(client, nurse_user, staff_password, room, ot_room):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get(
        "/api/v1/rooms",
        params={"branch_id": str(nurse_user.branch_id), "room_type": "ot"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(ot_room.id)}


def test_list_rooms_403_cross_branch(client, nurse_user, staff_password, other_branch):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/rooms", params={"branch_id": str(other_branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/drugs
# ---------------------------------------------------------------------------


def test_list_drugs_200_no_query_returns_rows(client, pharmacist_user, staff_password):
    """Unfiltered browse-all is capped (`_DRUG_LIST_LIMIT`) and this table is
    deliberately never truncated between tests, so a fresh per-test fixture
    row has no guaranteed spot in that capped alphabetical slice -- assert
    against a real, always-present seeded drug name instead (same "Coumadin"
    marker `conftest.py`'s own seed-data check uses)."""
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.get("/api/v1/drugs", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    names = {row["name"] for row in resp.json()}
    assert "Coumadin" in names


def test_list_drugs_query_filters_by_partial_name(client, pharmacist_user, staff_password, drug, other_drug):
    """Query on the fixture's unique hex suffix, not its shared "Test Dru..."
    prefix -- `other_drug.name` ("Other Test Drug <hex>") also contains
    "Test Dru" as a substring, so filtering on that prefix does not actually
    disambiguate the two fixtures under a substring (ILIKE %...%) search."""
    token = _login(client, pharmacist_user.email, staff_password)

    unique_suffix = drug.name.rsplit(" ", maxsplit=1)[-1]
    resp = client.get("/api/v1/drugs", params={"query": unique_suffix}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    names = {row["name"] for row in resp.json()}
    assert drug.name in names
    assert other_drug.name not in names


def test_list_drugs_403_patient(client, patient_user, patient_password):
    token = _login(client, patient_user.email, patient_password, url="/auth/patient/login")

    resp = client.get("/api/v1/drugs", headers=_auth(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/staff
# ---------------------------------------------------------------------------


def test_list_staff_200_system_admin(client, system_admin_user, staff_password, front_desk_user, nurse_user):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/staff", params={"branch_id": str(system_admin_user.branch_id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(front_desk_user.id) in ids
    assert str(nurse_user.id) in ids


def test_list_staff_filters_by_role(client, system_admin_user, staff_password, front_desk_user, nurse_user):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/staff",
        params={"branch_id": str(system_admin_user.branch_id), "role": "nurse"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {row["id"] for row in body} == {str(nurse_user.id)}
    assert all(row["role"] == "nurse" for row in body)


@pytest.mark.parametrize("role_fixture", ["front_desk_user", "nurse_user", "billing_admin_user"])
def test_list_staff_403_non_admin_roles(client, request, role_fixture, staff_password, branch):
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.get("/api/v1/staff", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


def test_list_staff_422_unknown_role_filter(client, system_admin_user, staff_password, branch):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/staff",
        params={"branch_id": str(branch.id), "role": "patient"},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text
