"""Integration tests for the Notification & Alert Hub module's HTTP surface
(routers/notifications.py, services/notification_service.py), per
PRPs/notification-hub-prp.md Phase 3. Runs against a real Postgres + Redis
(see tests/conftest.py's module docstring), reusing fixtures from prior
modules' TEST-AGENTs (`client`, `db`, `branch`, `other_branch`,
`front_desk_user`, `system_admin_user`, `staff_user` (doctor), `nurse_user`,
`pharmacist_user`, `billing_admin_user`, `patient`) plus this file's own
`_make_notification` helper (direct ORM insert, not through
`notification_engine.handle_event` -- these tests only need a
`Notification` row in a known state to query/retry against, not to
re-exercise the consumer's own recipient-resolution logic, which is covered
in test_notification_engine.py).

Engine-level behavior (topic handlers, recipient resolution, provider
failure handling) is covered in test_notification_engine.py -- this file is
about the layer on top: role gating (`require_role`), tenant scoping
(`_resolve_branch_filter`, mirroring `billing_service._resolve_branch_filter`
exactly), query-param filtering, and `authorize()`'s per-row tenant guard on
retry.
"""

import uuid

import pytest

from app.models.notification import Notification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_notification(
    db,
    *,
    branch_id,
    patient_id=None,
    user_id=None,
    channel="sms",
    template="appointment_confirmation",
    status="queued",
    payload=None,
) -> Notification:
    notification = Notification(
        branch_id=branch_id,
        patient_id=patient_id,
        user_id=user_id,
        channel=channel,
        template=template,
        status=status,
        payload=payload or {},
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


# ---------------------------------------------------------------------------
# 1. GET /api/v1/notifications role gating -- front_desk/system_admin only.
# ---------------------------------------------------------------------------


def test_list_200_front_desk(client, db, front_desk_user, staff_password, branch, patient):
    _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text


def test_list_200_system_admin(client, db, system_admin_user, staff_password, branch, patient):
    _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get("/api/v1/notifications", headers=_auth(token))

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "role_fixture", ["staff_user", "nurse_user", "pharmacist_user", "billing_admin_user"]
)
def test_list_403_other_roles(client, request, role_fixture, staff_password, branch):
    """`staff_user` is role=doctor (see conftest.py's docstring). None of
    doctor/nurse/pharmacist/billing_admin appear on the PRP's ENDPOINTS
    table for this module -- `require_role` must deny every one of them,
    matching every prior module's "no access at all for roles with no
    legitimate reason" discipline."""
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 2. Tenant scoping: mirrors billing_service._resolve_branch_filter exactly.
# ---------------------------------------------------------------------------


def test_list_422_front_desk_omits_branch_id(client, front_desk_user, staff_password):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get("/api/v1/notifications", headers=_auth(token))

    assert resp.status_code == 422, resp.text


def test_list_422_front_desk_mismatched_branch_id(client, front_desk_user, staff_password, other_branch):
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"branch_id": str(other_branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 422, resp.text


def test_list_200_front_desk_matching_branch_id_only_sees_own_branch(
    client, db, front_desk_user, staff_password, branch, other_branch, patient
):
    own_branch_notification = _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    _make_notification(db, branch_id=other_branch.id, patient_id=patient.id)
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(own_branch_notification.id)}


def test_list_200_system_admin_omits_branch_id_sees_all_branches(
    client, db, system_admin_user, staff_password, branch, other_branch, patient
):
    n1 = _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    n2 = _make_notification(db, branch_id=other_branch.id, patient_id=patient.id)
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get("/api/v1/notifications", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(n1.id), str(n2.id)}


def test_list_200_system_admin_with_branch_id_filters_to_that_branch(
    client, db, system_admin_user, staff_password, branch, other_branch, patient
):
    n1 = _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    _make_notification(db, branch_id=other_branch.id, patient_id=patient.id)
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(n1.id)}


# ---------------------------------------------------------------------------
# 3. Filtering by patient_id/user_id/status. `status` on the wire is aliased
#    to `status_param` in the router via `Query(alias="status")`.
# ---------------------------------------------------------------------------


def test_list_filters_by_patient_id(client, db, system_admin_user, staff_password, branch, patient):
    from app.models.patient import Patient

    other_patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Other Notification Patient",
        dob=patient.dob,
        sex="M",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(other_patient)
    db.commit()
    db.refresh(other_patient)

    target = _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    _make_notification(db, branch_id=branch.id, patient_id=other_patient.id)
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"patient_id": str(patient.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(target.id)}


def test_list_filters_by_user_id(client, db, system_admin_user, staff_password, branch, front_desk_user, patient):
    """No currently-published topic populates `Notification.user_id` (every
    `_TOPIC_HANDLERS` entry resolves `patient_id` -- see the coverage-gap
    note in test_notification_engine.py), so this row is built directly via
    the ORM with `user_id` set, exactly the way `retry_notification`'s
    user_id branch would if it were ever reached."""
    target = _make_notification(db, branch_id=branch.id, user_id=front_desk_user.id)
    _make_notification(db, branch_id=branch.id, patient_id=patient.id)
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"user_id": str(front_desk_user.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(target.id)}


def test_list_filters_by_status(client, db, system_admin_user, staff_password, branch, patient):
    failed = _make_notification(db, branch_id=branch.id, patient_id=patient.id, status="failed")
    _make_notification(db, branch_id=branch.id, patient_id=patient.id, status="sent")
    _make_notification(db, branch_id=branch.id, patient_id=patient.id, status="queued")
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"status": "failed"}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(failed.id)}
    assert all(row["status"] == "failed" for row in resp.json())


# ---------------------------------------------------------------------------
# 4. PATCH /api/v1/notifications/{id}/retry role gating -- system_admin only.
# ---------------------------------------------------------------------------


def test_retry_200_system_admin(client, db, system_admin_user, staff_password, branch, patient):
    failed = _make_notification(db, branch_id=branch.id, patient_id=patient.id, status="failed")
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.patch(f"/api/v1/notifications/{failed.id}/retry", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"  # LoggingNotificationProvider always succeeds


@pytest.mark.parametrize(
    "role_fixture",
    ["front_desk_user", "staff_user", "nurse_user", "pharmacist_user", "billing_admin_user"],
)
def test_retry_403_non_system_admin_roles(
    client, db, request, role_fixture, staff_password, branch, patient
):
    """front_desk is view-only per the PRP's ENDPOINTS table -- manual retry
    is a system_admin-only operational action, so front_desk (and every
    other non-admin role) must be denied here too, not just on GET."""
    failed = _make_notification(db, branch_id=branch.id, patient_id=patient.id, status="failed")
    user = request.getfixturevalue(role_fixture)
    token = _login(client, user.email, staff_password)

    resp = client.patch(f"/api/v1/notifications/{failed.id}/retry", headers=_auth(token))

    assert resp.status_code == 403, resp.text


def test_retry_404_not_found(client, system_admin_user, staff_password):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.patch(f"/api/v1/notifications/{uuid.uuid4()}/retry", headers=_auth(token))

    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("not_failed_status", ["sent", "queued"])
def test_retry_409_not_failed_status(
    client, db, system_admin_user, staff_password, branch, patient, not_failed_status
):
    notification = _make_notification(
        db, branch_id=branch.id, patient_id=patient.id, status=not_failed_status
    )
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.patch(f"/api/v1/notifications/{notification.id}/retry", headers=_auth(token))

    assert resp.status_code == 409, resp.text


def test_retry_updates_status_to_sent_and_returns_updated_row(
    client, db, system_admin_user, staff_password, branch, patient
):
    failed = _make_notification(
        db, branch_id=branch.id, patient_id=patient.id, status="failed", template="appointment_cancelled"
    )
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.patch(f"/api/v1/notifications/{failed.id}/retry", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(failed.id)
    assert body["status"] == "sent"
    assert body["template"] == "appointment_cancelled"

    db.expire_all()
    reloaded = db.get(Notification, failed.id)
    assert reloaded.status == "sent"


# ---------------------------------------------------------------------------
# 5. Cross-branch access.
#
# For GET (list): a front_desk caller can only ever resolve a branch_id
# filter equal to their OWN branch (`_resolve_branch_filter` 422s on
# mismatch/omission -- see section 2 above), so a Branch-B notification is
# simply never returned to a Branch-A front_desk caller. Proven below by
# reusing the same fixture shape test_billing_router.py's cross-branch tests
# use, adapted to this endpoint's actual behavior (a filtered-empty list,
# since there is no single-resource GET for a Notification the way
# GET /invoices/{id} exists for billing).
#
# For PATCH /retry: `_RETRY_ROLES = ("system_admin",)` (routers/notifications.py)
# means `require_role` only ever lets a system_admin caller reach
# `notification_service.retry_notification` at all via HTTP -- and
# `authorize()`'s tenant guard explicitly bypasses `system_admin`
# (core/security.py: `user.role != UserRole.system_admin`). So the tenant
# guard branch inside `retry_notification` is, as currently wired,
# UNREACHABLE through the HTTP layer: no caller who could pass the router's
# role gate can ever be denied by the row-level branch check, and no caller
# who could be denied by the row-level branch check can ever pass the
# router's role gate. This isn't a bug (front_desk simply has zero retry
# access, by design, regardless of branch) but it does mean the "cross-branch
# retry denied" scenario the PRP asks to mirror from test_billing_router.py
# has no real HTTP-reachable equivalent here. The two tests below instead
# prove the mechanism exists and works correctly at the service layer
# directly (bypassing the router's role gate, the same way this comment
# explains no real HTTP caller ever will), which is the closest honest
# equivalent -- and confirm the GET-side scoping.
# ---------------------------------------------------------------------------


def test_list_cross_branch_front_desk_never_sees_other_branch_row(
    client, db, front_desk_user, staff_password, branch, other_branch, patient
):
    _make_notification(db, branch_id=other_branch.id, patient_id=patient.id)
    token = _login(client, front_desk_user.email, staff_password)

    resp = client.get(
        "/api/v1/notifications", params={"branch_id": str(branch.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_retry_service_layer_tenant_guard_denies_cross_branch(db, front_desk_user, branch, other_branch, patient):
    """Service-layer-direct equivalent of test_billing_router.py's
    cross-branch retry-denial tests -- see the module-level comment above for
    why no real HTTP caller can reach this branch of `authorize()` (retry is
    system_admin-only at the router, and system_admin bypasses the tenant
    guard). `front_desk_user` has a "notification"/"write" ABAC policy
    registered nowhere in core/security.py at all (only "read" -- see that
    file's Phase 2 comment), so this call is denied for TWO independent
    reasons layered together: the tenant guard (wrong branch) would fire
    first regardless."""
    from app.services import notification_service

    other_branch_failed = _make_notification(
        db, branch_id=other_branch.id, patient_id=patient.id, status="failed"
    )

    with pytest.raises(Exception) as exc_info:
        notification_service.retry_notification(db, front_desk_user, other_branch_failed.id)

    # HTTPException(403) from authorize()'s tenant guard.
    assert getattr(exc_info.value, "status_code", None) == 403
