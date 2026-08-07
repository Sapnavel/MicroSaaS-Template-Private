"""Integration tests for the Ward/Bed/OT module's HTTP surface
(routers/wards.py, services/ward_service.py), per
PRPs/ward-bed-ot-module-prp.md Phase 3. Runs against a real Postgres + Redis
(see tests/conftest.py's module docstring), reusing fixtures from prior
modules' TEST-AGENTs (`client`, `branch`, `other_branch`, `nurse_user`,
`front_desk_user`, `staff_user`/`doctor_record`, `other_doctor_user`/
`other_doctor_record`, `patient`, `system_admin_user`, `room`) plus this
module's own `ward`/`bed`/`other_bed`/`other_branch_ward`/`other_branch_bed`/
`ot_room`/`other_ot_room`.

Engine-level behavior (admit/discharge/transfer/set_bed_status/schedule_ot
happy paths, conflict detection, H1/H2 fixes, concurrency) is covered in
test_ward_engine.py -- this file is about the layer on top: does the router
map each `ward_engine` exception to the right status code and body shape,
does `require_role` actually gate each endpoint per the ENDPOINTS table, and
does `authorize()`'s tenant guard actually block cross-branch access.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.locking import LockAcquisitionError
from app.services import ward_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admit(client, token, patient_id, bed_id, start_time=None) -> "object":
    start_time = start_time or datetime.now(timezone.utc)
    return client.post(
        "/api/v1/wards/admissions",
        json={"patient_id": str(patient_id), "bed_id": str(bed_id), "start_time": start_time.isoformat()},
        headers=_auth(token),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/wards/beds
# ---------------------------------------------------------------------------


def test_list_beds_200_nurse_own_branch(client, nurse_user, staff_password, bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards/beds", params={"branch_id": str(nurse_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    bed_ids = {row["id"] for row in resp.json()}
    assert str(bed.id) in bed_ids


def test_list_beds_422_nurse_omits_required_branch_id(client, nurse_user, staff_password, bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards/beds", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_beds_422_nurse_mismatched_branch_id(client, nurse_user, staff_password, other_branch, bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards/beds", params={"branch_id": str(other_branch.id)}, headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_beds_system_admin_omits_branch_id_sees_all_branches(
    client, system_admin_user, staff_password, bed, other_branch_bed
):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get("/api/v1/wards/beds", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    bed_ids = {row["id"] for row in resp.json()}
    assert str(bed.id) in bed_ids
    assert str(other_branch_bed.id) in bed_ids


def test_list_beds_front_desk_can_read(client, front_desk_user, staff_password, bed):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/wards/beds", params={"branch_id": str(front_desk_user.branch_id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text


def test_list_beds_active_admission_id_null_when_unoccupied(client, nurse_user, staff_password, bed):
    """Frontend UUID-to-dropdown conversion follow-up: `active_admission_id`
    is additive on this existing response -- `None` for a bed with no open
    admission."""
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards/beds", params={"branch_id": str(nurse_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["id"] == str(bed.id))
    assert row["active_admission_id"] is None


def test_list_beds_active_admission_id_set_when_occupied(client, nurse_user, staff_password, bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]

    resp = client.get("/api/v1/wards/beds", params={"branch_id": str(nurse_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["id"] == str(bed.id))
    assert row["active_admission_id"] == admission_id


def test_list_beds_active_admission_id_null_again_after_discharge(client, nurse_user, staff_password, bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]
    discharge_resp = client.patch(f"/api/v1/wards/admissions/{admission_id}/discharge", json={}, headers=_auth(token))
    assert discharge_resp.status_code == 200, discharge_resp.text

    resp = client.get("/api/v1/wards/beds", params={"branch_id": str(nurse_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["id"] == str(bed.id))
    assert row["active_admission_id"] is None


# ---------------------------------------------------------------------------
# GET /api/v1/wards (frontend UUID-to-dropdown conversion follow-up)
# ---------------------------------------------------------------------------


def test_list_wards_200_own_branch(client, nurse_user, staff_password, ward):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards", params={"branch_id": str(nurse_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(ward.id)
    assert body[0]["ward_type"] == "icu"


def test_list_wards_422_missing_branch_id(client, nurse_user, staff_password, ward):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_wards_422_mismatched_branch_id(client, nurse_user, staff_password, other_branch, ward):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.get("/api/v1/wards", params={"branch_id": str(other_branch.id)}, headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_wards_403_lab_tech_cannot_read(client, lab_tech_user, staff_password, ward):
    """`lab_tech` is not in `_BED_READ_ROLES` -- same read-roles as
    `GET /wards/beds`."""
    token = _login(client, lab_tech_user.email, staff_password)

    resp = client.get("/api/v1/wards", params={"branch_id": str(lab_tech_user.branch_id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# POST /api/v1/wards/admissions
# ---------------------------------------------------------------------------


def test_create_admission_201_happy_path(client, nurse_user, staff_password, bed, patient):
    token = _login(client, nurse_user.email, staff_password)

    resp = _admit(client, token, patient.id, bed.id)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["bed_id"] == str(bed.id)
    assert body["patient_id"] == str(patient.id)
    assert body["discharged_at"] is None


def test_create_admission_403_front_desk_cannot_write(client, front_desk_user, staff_password, bed, patient):
    """front_desk only ever reads the bed matrix per the ENDPOINTS table --
    `require_role` gates this at the dependency level before the request
    body is even inspected."""
    token = _login(client, front_desk_user.email, staff_password)

    resp = _admit(client, token, patient.id, bed.id)

    assert resp.status_code == 403, resp.text


def test_create_admission_404_bed_not_found(client, nurse_user, staff_password, patient):
    token = _login(client, nurse_user.email, staff_password)

    resp = _admit(client, token, patient.id, uuid.uuid4())

    assert resp.status_code == 404, resp.text


def test_create_admission_404_patient_not_found(client, nurse_user, staff_password, bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = _admit(client, token, uuid.uuid4(), bed.id)

    assert resp.status_code == 404, resp.text


def test_create_admission_409_bed_not_available(client, nurse_user, staff_password, bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    first = _admit(client, token, patient.id, bed.id)
    assert first.status_code == 201, first.text

    second = _admit(client, token, patient.id, bed.id)

    assert second.status_code == 409, second.text


def test_create_admission_403_cross_branch_bed(client, nurse_user, staff_password, other_branch_bed, patient):
    """A nurse at `branch` must never admit into a bed under `other_branch`'s
    ward -- `authorize()`'s tenant guard, resolved by walking
    Bed -> Ward -> branch_id."""
    token = _login(client, nurse_user.email, staff_password)

    resp = _admit(client, token, patient.id, other_branch_bed.id)

    assert resp.status_code == 403, resp.text


def test_create_admission_system_admin_can_write_any_branch(client, system_admin_user, staff_password, other_branch_bed, patient):
    token = _login(client, system_admin_user.email, staff_password)

    resp = _admit(client, token, patient.id, other_branch_bed.id)

    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# PATCH /api/v1/wards/admissions/{id}/discharge
# ---------------------------------------------------------------------------


def test_discharge_admission_200(client, nurse_user, staff_password, bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]

    resp = client.patch(f"/api/v1/wards/admissions/{admission_id}/discharge", json={}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["discharged_at"] is not None


def test_discharge_admission_404_unknown_id(client, nurse_user, staff_password):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/admissions/{uuid.uuid4()}/discharge", json={}, headers=_auth(token)
    )

    assert resp.status_code == 404, resp.text


def test_discharge_admission_409_already_discharged(client, nurse_user, staff_password, bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]
    first = client.patch(f"/api/v1/wards/admissions/{admission_id}/discharge", json={}, headers=_auth(token))
    assert first.status_code == 200, first.text

    second = client.patch(f"/api/v1/wards/admissions/{admission_id}/discharge", json={}, headers=_auth(token))

    assert second.status_code == 409, second.text


def test_discharge_admission_403_cross_branch(client, nurse_user, staff_password, other_branch_bed, patient, system_admin_user):
    """The admission belongs to `other_branch` (admitted by system_admin, who
    can cross branches) -- `nurse_user` (scoped to `branch`) must be denied."""
    admin_token = _login(client, system_admin_user.email, staff_password)
    admission_id = _admit(client, admin_token, patient.id, other_branch_bed.id).json()["id"]

    nurse_token = _login(client, nurse_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/wards/admissions/{admission_id}/discharge", json={}, headers=_auth(nurse_token)
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# PATCH /api/v1/wards/admissions/{id}/transfer
# ---------------------------------------------------------------------------


def test_transfer_admission_200(client, nurse_user, staff_password, bed, other_bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]

    resp = client.patch(
        f"/api/v1/wards/admissions/{admission_id}/transfer",
        json={"new_bed_id": str(other_bed.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bed_id"] == str(other_bed.id)
    assert body["discharged_at"] is None


def test_transfer_admission_404_unknown_admission(client, nurse_user, staff_password, other_bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/admissions/{uuid.uuid4()}/transfer",
        json={"new_bed_id": str(other_bed.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


def test_transfer_admission_409_already_discharged(client, nurse_user, staff_password, bed, other_bed, patient):
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]
    discharge_resp = client.patch(f"/api/v1/wards/admissions/{admission_id}/discharge", json={}, headers=_auth(token))
    assert discharge_resp.status_code == 200, discharge_resp.text

    resp = client.patch(
        f"/api/v1/wards/admissions/{admission_id}/transfer",
        json={"new_bed_id": str(other_bed.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text


def test_transfer_admission_403_cross_branch_new_bed(client, nurse_user, staff_password, bed, other_branch_bed, patient):
    """Transfer must check BOTH the old AND new bed's ward -- a nurse at
    `branch` may hold a legitimate admission on their own `bed`, but must
    still be denied transferring it into `other_branch`'s bed."""
    token = _login(client, nurse_user.email, staff_password)
    admission_id = _admit(client, token, patient.id, bed.id).json()["id"]

    resp = client.patch(
        f"/api/v1/wards/admissions/{admission_id}/transfer",
        json={"new_bed_id": str(other_branch_bed.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# PATCH /api/v1/wards/beds/{id}/status
# ---------------------------------------------------------------------------


def test_update_bed_status_200_nurse(client, nurse_user, staff_password, bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{bed.id}/status", json={"status": "blocked"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "blocked"


def test_update_bed_status_403_doctor_cannot_write(client, staff_user, staff_password, bed):
    """Only nurse/system_admin may PATCH bed status per the ENDPOINTS table
    -- doctor is excluded even though doctor CAN admit/discharge/transfer."""
    token = _login(client, staff_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{bed.id}/status", json={"status": "blocked"}, headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text


def test_update_bed_status_403_front_desk_cannot_write(client, front_desk_user, staff_password, bed):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{bed.id}/status", json={"status": "blocked"}, headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text


def test_update_bed_status_404_unknown_bed(client, nurse_user, staff_password):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{uuid.uuid4()}/status", json={"status": "blocked"}, headers=_auth(token)
    )

    assert resp.status_code == 404, resp.text


def test_update_bed_status_409_illegal_transition_hand_built_body(client, nurse_user, staff_password, bed):
    """`bed` starts `available` -- `available -> available` (a no-op re-request)
    is not in the legal transition set, and the 409 body must be hand-built
    with `current`/`requested` alongside `detail` (NOT the plain
    `{"detail": ...}` HTTPException shape)."""
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{bed.id}/status", json={"status": "available"}, headers=_auth(token)
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["current"] == "available"
    assert body["requested"] == "available"
    assert "detail" in body


def test_update_bed_status_422_occupied_and_cleaning_rejected_at_wire_level(client, nurse_user, staff_password, bed):
    """`occupied`/`cleaning` are never legal targets of this endpoint at all
    -- the `Literal["available", "blocked"]` on `BedStatusUpdateRequest`
    rejects them with a plain 422 before the engine is ever reached."""
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{bed.id}/status", json={"status": "occupied"}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text


def test_update_bed_status_403_cross_branch(client, nurse_user, staff_password, other_branch_bed):
    token = _login(client, nurse_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/wards/beds/{other_branch_bed.id}/status", json={"status": "blocked"}, headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# POST /api/v1/wards/ot-schedule
# ---------------------------------------------------------------------------


def _schedule_ot(client, token, room_id, patient_id, surgeon_id, start_time=None, duration_minutes=60) -> "object":
    start_time = start_time or (datetime.now(timezone.utc) + timedelta(hours=1))
    return client.post(
        "/api/v1/wards/ot-schedule",
        json={
            "room_id": str(room_id),
            "patient_id": str(patient_id),
            "surgeon_id": str(surgeon_id),
            "start_time": start_time.isoformat(),
            "duration_minutes": duration_minutes,
        },
        headers=_auth(token),
    )


def test_create_ot_schedule_201_doctor(client, staff_user, staff_password, ot_room, patient, doctor_record):
    token = _login(client, staff_user.email, staff_password)

    resp = _schedule_ot(client, token, ot_room.id, patient.id, doctor_record.id)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["room_id"] == str(ot_room.id)
    assert body["surgeon_id"] == str(doctor_record.id)


def test_create_ot_schedule_403_nurse_cannot_write(client, nurse_user, staff_password, ot_room, patient, doctor_record):
    token = _login(client, nurse_user.email, staff_password)

    resp = _schedule_ot(client, token, ot_room.id, patient.id, doctor_record.id)

    assert resp.status_code == 403, resp.text


def test_create_ot_schedule_404_room_not_found(client, staff_user, staff_password, patient, doctor_record):
    token = _login(client, staff_user.email, staff_password)

    resp = _schedule_ot(client, token, uuid.uuid4(), patient.id, doctor_record.id)

    assert resp.status_code == 404, resp.text


def test_create_ot_schedule_404_surgeon_not_found(client, staff_user, staff_password, ot_room, patient):
    token = _login(client, staff_user.email, staff_password)

    resp = _schedule_ot(client, token, ot_room.id, patient.id, uuid.uuid4())

    assert resp.status_code == 404, resp.text


def test_create_ot_schedule_409_room_conflict_hand_built_body_with_reason(
    client, staff_user, staff_password, ot_room, patient, doctor_record, other_doctor_record
):
    token = _login(client, staff_user.email, staff_password)
    start = datetime.now(timezone.utc) + timedelta(hours=6)
    first = _schedule_ot(client, token, ot_room.id, patient.id, doctor_record.id, start_time=start)
    assert first.status_code == 201, first.text

    second = _schedule_ot(
        client, token, ot_room.id, patient.id, other_doctor_record.id, start_time=start + timedelta(minutes=15)
    )

    assert second.status_code == 409, second.text
    body = second.json()
    assert body["reason"] == "room_conflict"
    assert "detail" in body


def test_create_ot_schedule_403_cross_branch_room(client, staff_user, staff_password, patient, doctor_record, other_branch):
    """OT scheduling authorizes against the `Room` directly -- a doctor at
    `branch` must be denied scheduling against a room under `other_branch`."""
    from app.models.resource import Room
    from tests.conftest import TestSessionLocal

    setup_db = TestSessionLocal()
    try:
        other_room = Room(branch_id=other_branch.id, name="Other OT", room_type="ot")
        setup_db.add(other_room)
        setup_db.commit()
        setup_db.refresh(other_room)
        other_room_id = other_room.id
    finally:
        setup_db.close()

    token = _login(client, staff_user.email, staff_password)
    resp = _schedule_ot(client, token, other_room_id, patient.id, doctor_record.id)

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/wards/ot-schedule
# ---------------------------------------------------------------------------


def test_list_ot_schedule_422_no_filters_supplied(client, staff_user, staff_password):
    token = _login(client, staff_user.email, staff_password)

    resp = client.get("/api/v1/wards/ot-schedule", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_ot_schedule_200_filtered_by_room_id(client, staff_user, staff_password, ot_room, patient, doctor_record):
    token = _login(client, staff_user.email, staff_password)
    created = _schedule_ot(client, token, ot_room.id, patient.id, doctor_record.id)
    assert created.status_code == 201, created.text

    resp = client.get("/api/v1/wards/ot-schedule", params={"room_id": str(ot_room.id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    room_ids = {row["room_id"] for row in resp.json()}
    assert room_ids == {str(ot_room.id)}


def test_list_ot_schedule_403_nurse_can_read_front_desk_cannot(
    client, nurse_user, front_desk_user, staff_password, ot_room, patient, doctor_record, staff_user
):
    token = _login(client, staff_user.email, staff_password)
    created = _schedule_ot(client, token, ot_room.id, patient.id, doctor_record.id)
    assert created.status_code == 201, created.text

    nurse_token = _login(client, nurse_user.email, staff_password)
    nurse_resp = client.get(
        "/api/v1/wards/ot-schedule", params={"room_id": str(ot_room.id)}, headers=_auth(nurse_token)
    )
    assert nurse_resp.status_code == 200, nurse_resp.text

    fd_token = _login(client, front_desk_user.email, staff_password)
    fd_resp = client.get("/api/v1/wards/ot-schedule", params={"room_id": str(ot_room.id)}, headers=_auth(fd_token))
    assert fd_resp.status_code == 403, fd_resp.text


# ---------------------------------------------------------------------------
# FINDING: lock contention is not translated to 423 anywhere in this module
# ---------------------------------------------------------------------------


def test_lock_contention_is_mapped_to_423(monkeypatch, client, nurse_user, staff_password, bed, patient):
    """Router-level consequence of the fix in
    test_ward_engine.py::test_lock_contention_propagates_as_ResourceBusyError:
    `ward_engine.py` now wraps every lock scope in
    `except LockAcquisitionError: raise ResourceBusyError`, so
    `routers/wards.py`'s existing `except ResourceBusyError -> 423` handler
    actually gets exercised on real lock contention instead of a raw
    `LockAcquisitionError` reaching FastAPI as an unhandled 500."""

    def _raise(*_args, **_kwargs):
        raise LockAcquisitionError("simulated lock contention")

    monkeypatch.setattr(ward_engine.lock_manager, "acquire_all", _raise)

    token = _login(client, nurse_user.email, staff_password)
    resp = _admit(client, token, patient.id, bed.id)
    assert resp.status_code == 423
