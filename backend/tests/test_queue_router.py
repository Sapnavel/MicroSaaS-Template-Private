"""Integration tests for the Real-Time Queue & Digital Token module's HTTP
surface (routers/queue.py, services/queue_service.py), per
PRPs/realtime-queue-prp.md Phase 3. Runs against a real Postgres + Redis (see
tests/conftest.py's module docstring), reusing fixtures from prior modules'
TEST-AGENTs (`client`, `db`, `branch`, `other_branch`, `patient`,
`in_progress_appointment`, `doctor_record`, `room`, `specialty`,
`front_desk_user`, `nurse_user`, `system_admin_user`, `staff_user` (doctor,
see conftest.py's docstring), `pharmacist_user`, `billing_admin_user`).

Service-layer behavior (state machine transitions, token-number computation,
RBAC/ABAC at the function level, the wait-time consumer's recompute
heuristic) is covered in test_queue_service.py -- this file is about the
layer on top: does `require_role` gate each endpoint per the PRP's ENDPOINTS
table, does the "exactly one shape" Pydantic validation actually 422 at the
wire, and does the router map each `queue_service` exception to the right
status code (404/409).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.queue import QueueToken, TokenStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_token(
    db,
    *,
    branch_id,
    department_id=None,
    status=TokenStatus.waiting,
    token_number=1,
    checked_in_at=None,
) -> QueueToken:
    token = QueueToken(
        branch_id=branch_id,
        department_id=department_id,
        status=status,
        token_number=token_number,
    )
    if checked_in_at is not None:
        token.checked_in_at = checked_in_at
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


# ---------------------------------------------------------------------------
# 1. POST /api/v1/queue/check-in -- role gating.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_fixture", ["front_desk_user", "nurse_user", "system_admin_user"])
def test_check_in_200_write_roles(client, request, role_fixture, staff_password, branch, specialty):
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"branch_id": str(branch.id), "department_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "waiting"
    assert body["token_number"] == 1
    assert body["appointment_id"] is None


@pytest.mark.parametrize("role_fixture", ["staff_user", "pharmacist_user", "billing_admin_user"])
def test_check_in_403_non_write_roles(client, request, role_fixture, staff_password, branch, specialty):
    """`staff_user` is role=doctor (see conftest.py's docstring), read-only
    per design decision #6."""
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"branch_id": str(branch.id), "department_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 2. POST /check-in -- "exactly one shape" 422 validation.
# ---------------------------------------------------------------------------


def test_check_in_422_both_shapes_supplied(
    client, front_desk_user, staff_password, branch, specialty, in_progress_appointment
):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={
            "appointment_id": str(in_progress_appointment.id),
            "branch_id": str(branch.id),
            "department_id": specialty.id,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_check_in_422_neither_shape_supplied(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post("/api/v1/queue/check-in", json={}, headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_check_in_422_partial_walk_in_branch_id_without_department_id(
    client, front_desk_user, staff_password, branch
):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"branch_id": str(branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_check_in_422_partial_walk_in_department_id_without_branch_id(
    client, front_desk_user, staff_password, specialty
):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"department_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# 3. POST /check-in -- success paths + not-found.
# ---------------------------------------------------------------------------


def test_check_in_200_appointment_linked_success(
    client, front_desk_user, staff_password, in_progress_appointment, doctor_record
):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"appointment_id": str(in_progress_appointment.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["appointment_id"] == str(in_progress_appointment.id)
    assert body["branch_id"] == str(in_progress_appointment.branch_id)
    assert body["department_id"] == doctor_record.specialty_id
    assert body["status"] == "waiting"


def test_check_in_200_walk_in_success(client, front_desk_user, staff_password, branch, specialty):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"branch_id": str(branch.id), "department_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["branch_id"] == str(branch.id)
    assert body["department_id"] == specialty.id
    assert body["appointment_id"] is None
    assert body["is_priority"] is False


def test_check_in_200_walk_in_priority(client, front_desk_user, staff_password, branch, specialty):
    """HMS Project Completion Prompt gap ("emergency queue priority")."""
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"branch_id": str(branch.id), "department_id": specialty.id, "is_priority": True},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_priority"] is True


def test_check_in_404_nonexistent_appointment(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"appointment_id": str(uuid.uuid4())},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 4. PATCH /api/v1/queue/{token_id}/status.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_fixture", ["front_desk_user", "nurse_user", "system_admin_user"])
def test_update_status_200_write_roles(client, db, request, role_fixture, staff_password, branch):
    user = request.getfixturevalue(role_fixture)
    token_row = _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)
    auth_token = _login(client, user.email, staff_password)

    resp = client.patch(
        f"/api/v1/queue/{token_row.id}/status",
        json={"status": "in_consultation"},
        headers=_auth(auth_token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_consultation"


@pytest.mark.parametrize("role_fixture", ["staff_user", "pharmacist_user", "billing_admin_user"])
def test_update_status_403_non_write_roles(client, db, request, role_fixture, staff_password, branch):
    user = request.getfixturevalue(role_fixture)
    token_row = _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)
    auth_token = _login(client, user.email, staff_password)

    resp = client.patch(
        f"/api/v1/queue/{token_row.id}/status",
        json={"status": "in_consultation"},
        headers=_auth(auth_token),
    )

    assert resp.status_code == 403, resp.text


def test_update_status_404_nonexistent_token(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/queue/{uuid.uuid4()}/status",
        json={"status": "in_consultation"},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


def test_update_status_409_illegal_transition(client, db, front_desk_user, staff_password, branch):
    token_row = _make_token(db, branch_id=branch.id, status=TokenStatus.done)
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/queue/{token_row.id}/status",
        json={"status": "waiting"},
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text


def test_update_status_200_legal_transition_updates_status(client, db, front_desk_user, staff_password, branch):
    token_row = _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/queue/{token_row.id}/status",
        json={"status": "delayed"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delayed"
    assert body["id"] == str(token_row.id)


# ---------------------------------------------------------------------------
# 5. GET /api/v1/queue/board.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_fixture", ["front_desk_user", "nurse_user", "staff_user", "system_admin_user"]
)
def test_board_200_read_roles_including_doctor(client, db, request, role_fixture, staff_password, branch):
    """`staff_user` is role=doctor -- read-only per design decision #6,
    included here since the board is the one endpoint doctor may reach."""
    _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.get("/api/v1/queue/board", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


@pytest.mark.parametrize("role_fixture", ["pharmacist_user", "billing_admin_user"])
def test_board_403_roles_with_no_queue_token_policy(client, request, role_fixture, staff_password, branch):
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.get("/api/v1/queue/board", params={"branch_id": str(branch.id)}, headers=_auth(token))

    assert resp.status_code == 403, resp.text


def test_board_422_branch_id_required(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/queue/board", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_board_department_id_optional_filter(client, db, front_desk_user, staff_password, branch, specialty):
    now = datetime.now(timezone.utc)
    matching = _make_token(
        db, branch_id=branch.id, department_id=specialty.id, checked_in_at=now - timedelta(minutes=5)
    )
    _make_token(db, branch_id=branch.id, department_id=None, checked_in_at=now - timedelta(minutes=1))
    token = _login(client, front_desk_user.email, staff_password)

    all_resp = client.get("/api/v1/queue/board", params={"branch_id": str(branch.id)}, headers=_auth(token))
    assert all_resp.status_code == 200, all_resp.text
    assert len(all_resp.json()) == 2

    filtered_resp = client.get(
        "/api/v1/queue/board",
        params={"branch_id": str(branch.id), "department_id": specialty.id},
        headers=_auth(token),
    )
    assert filtered_resp.status_code == 200, filtered_resp.text
    body = filtered_resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(matching.id)


# ---------------------------------------------------------------------------
# 6. Cross-branch: a token belonging to `other_branch` cannot be transitioned
#    by a caller scoped to `branch`.
# ---------------------------------------------------------------------------


def test_update_status_403_cross_branch(client, db, front_desk_user, staff_password, other_branch):
    other_branch_token = _make_token(db, branch_id=other_branch.id, status=TokenStatus.waiting)
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.patch(
        f"/api/v1/queue/{other_branch_token.id}/status",
        json={"status": "in_consultation"},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text


def test_check_in_403_cross_branch_walk_in(client, front_desk_user, staff_password, other_branch, specialty):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.post(
        "/api/v1/queue/check-in",
        json={"branch_id": str(other_branch.id), "department_id": specialty.id},
        headers=_auth(token),
    )

    assert resp.status_code == 403, resp.text
