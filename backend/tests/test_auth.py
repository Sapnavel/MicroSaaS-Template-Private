"""Integration tests for the auth endpoints (routers/auth.py, routers/admin.py)
per PRPs/auth-rbac-abac-prp.md Phase 3. Runs against a real Postgres + Redis
(see tests/conftest.py's module docstring for the infra rationale)."""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app.config import settings as app_settings
from app.core import security
from tests.conftest import unique_email

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_patient_success(client):
    resp = client.post(
        "/auth/register",
        json={
            "email": unique_email("newpatient"),
            "password": "a-strong-password-1",
            "full_name": "New Patient",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "patient"
    assert "id" in body


def test_register_rejects_client_supplied_role(client):
    """extra="forbid" on RegisterRequest: any request carrying a `role`
    field at all is rejected with 422, matching or not."""
    resp = client.post(
        "/auth/register",
        json={
            "email": unique_email("sneaky"),
            "password": "a-strong-password-1",
            "full_name": "Sneaky Patient",
            "role": "system_admin",
        },
    )
    assert resp.status_code == 422


def test_register_rejects_client_supplied_branch_id(client):
    resp = client.post(
        "/auth/register",
        json={
            "email": unique_email("sneaky2"),
            "password": "a-strong-password-1",
            "full_name": "Sneaky Patient 2",
            "branch_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Staff / patient login
# ---------------------------------------------------------------------------


def test_staff_login_success(client, staff_user, staff_password):
    resp = client.post("/auth/login", json={"email": staff_user.email, "password": staff_password})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


def test_staff_login_wrong_password_and_unknown_email_share_message(client, staff_user):
    """OWASP: must not leak whether the failure was 'no such account' vs
    'wrong password' — both paths go through authenticate_user()'s single
    generic 401 message."""
    wrong_password_resp = client.post(
        "/auth/login", json={"email": staff_user.email, "password": "definitely-wrong-pw"}
    )
    unknown_email_resp = client.post(
        "/auth/login", json={"email": unique_email("ghost"), "password": "whatever-password-1"}
    )

    assert wrong_password_resp.status_code == 401
    assert unknown_email_resp.status_code == 401
    assert wrong_password_resp.json()["detail"] == unknown_email_resp.json()["detail"]


def test_staff_login_rejects_patient_account(client, patient_user, patient_password):
    resp = client.post("/auth/login", json={"email": patient_user.email, "password": patient_password})
    assert resp.status_code == 401


def test_patient_login_rejects_staff_account(client, staff_user, staff_password):
    resp = client.post(
        "/auth/patient/login", json={"email": staff_user.email, "password": staff_password}
    )
    assert resp.status_code == 401


def test_patient_login_success(client, patient_user, patient_password):
    resp = client.post(
        "/auth/patient/login", json={"email": patient_user.email, "password": patient_password}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------


def test_me_with_valid_token(client, staff_user, staff_password):
    login = client.post("/auth/login", json={"email": staff_user.email, "password": staff_password})
    token = login.json()["access_token"]

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == staff_user.email
    assert body["id"] == str(staff_user.id)
    assert body["role"] == "doctor"


def test_me_without_token(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Refresh rotation
# ---------------------------------------------------------------------------


def test_refresh_rotates_and_old_token_is_rejected(client, staff_user, staff_password):
    login = client.post("/auth/login", json={"email": staff_user.email, "password": staff_password})
    original_refresh = login.json()["refresh_token"]

    first_refresh = client.post("/auth/refresh", json={"refresh_token": original_refresh})
    assert first_refresh.status_code == 200
    new_pair = first_refresh.json()
    assert new_pair["refresh_token"] != original_refresh

    reuse_resp = client.post("/auth/refresh", json={"refresh_token": original_refresh})
    assert reuse_resp.status_code == 401


# ---------------------------------------------------------------------------
# Logout / blocklist
# ---------------------------------------------------------------------------


def test_logout_blocklists_access_token_and_revokes_refresh_token(client, staff_user, staff_password):
    login = client.post("/auth/login", json={"email": staff_user.email, "password": staff_password})
    tokens = login.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    logout_resp = client.post(
        "/auth/logout",
        json={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout_resp.status_code == 204

    me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_resp.status_code == 401

    refresh_resp = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_login_rate_limit_triggers_after_configured_attempts(client):
    email = unique_email("bruteforced")
    limit = app_settings.login_rate_limit_per_minute
    assert limit == 3, "conftest sets LOGIN_RATE_LIMIT_PER_MINUTE=3 for a fast test"

    statuses = []
    for _ in range(limit + 2):
        resp = client.post("/auth/login", json={"email": email, "password": "irrelevant-pw"})
        statuses.append(resp.status_code)

    # First `limit` attempts are rejected on credentials (401); everything
    # after that is rejected by the rate limiter (429) before credentials
    # are even checked.
    assert statuses[:limit] == [401] * limit
    assert 429 in statuses[limit:]


# ---------------------------------------------------------------------------
# Admin staff provisioning
# ---------------------------------------------------------------------------


def test_admin_can_provision_staff(client, system_admin_user, staff_password, branch):
    login = client.post(
        "/auth/login", json={"email": system_admin_user.email, "password": staff_password}
    )
    admin_token = login.json()["access_token"]

    resp = client.post(
        "/admin/staff",
        json={
            "email": unique_email("newnurse"),
            "password": "a-fresh-strong-pw-1",
            "full_name": "Nurse Joy",
            "role": "nurse",
            "branch_id": str(branch.id),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "nurse"
    assert body["branch_id"] == str(branch.id)


def test_admin_staff_provisioning_forbidden_for_non_admin(client, staff_user, staff_password, branch):
    login = client.post("/auth/login", json={"email": staff_user.email, "password": staff_password})
    token = login.json()["access_token"]

    resp = client.post(
        "/admin/staff",
        json={
            "email": unique_email("blocked"),
            "password": "a-fresh-strong-pw-2",
            "full_name": "Should Not Be Created",
            "role": "nurse",
            "branch_id": str(branch.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_admin_staff_provisioning_rejects_patient_role(
    client, system_admin_user, staff_password, branch
):
    login = client.post(
        "/auth/login", json={"email": system_admin_user.email, "password": staff_password}
    )
    admin_token = login.json()["access_token"]

    resp = client.post(
        "/admin/staff",
        json={
            "email": unique_email("nopatient"),
            "password": "a-fresh-strong-pw-3",
            "full_name": "Nope",
            "role": "patient",
            "branch_id": str(branch.id),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


def test_token_signed_with_older_retained_kid_still_verifies(client, staff_user):
    """conftest configures TWO kids (k1, k2) with jwt_current_kid=k2. A
    token signed under k1 (older, but still present in the keyring) must
    still authenticate successfully -- this is the whole point of keeping
    an old kid around during a rotation's overlap window."""
    original_kid = app_settings.jwt_current_kid
    assert "k1" in app_settings.jwt_signing_keys and "k1" != original_kid

    app_settings.jwt_current_kid = "k1"
    try:
        old_kid_token = security.create_access_token(
            str(staff_user.id), role=staff_user.role.value, branch_id=None, hospital_group_id=None
        )
    finally:
        app_settings.jwt_current_kid = original_kid

    header = pyjwt.get_unverified_header(old_kid_token)
    assert header["kid"] == "k1"

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {old_kid_token}"})
    assert resp.status_code == 200


def test_token_with_unknown_kid_is_rejected(client, staff_user):
    payload = {
        "sub": str(staff_user.id),
        "jti": "unknown-kid-test",
        "type": "access",
        "role": "doctor",
        "branch_id": None,
        "hospital_group_id": None,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    bogus_token = pyjwt.encode(
        payload, "some-key-not-in-the-keyring", algorithm="HS256", headers={"kid": "kid-does-not-exist"}
    )

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {bogus_token}"})
    assert resp.status_code == 401


def test_decode_token_removed_kid_fails_once_dropped_from_keyring(staff_user):
    """Unit-level check of the removal half of the rotation story: once a
    kid is dropped from settings.jwt_signing_keys, a token signed under it
    (even if signed earlier while it was still current) fails to decode."""
    original_keys = dict(app_settings.jwt_signing_keys)
    original_kid = app_settings.jwt_current_kid

    app_settings.jwt_current_kid = "k1"
    token = security.create_access_token(str(staff_user.id), role="doctor")

    # Now simulate the kid having been retired/removed from the keyring.
    app_settings.jwt_signing_keys = {"k2": original_keys["k2"]}
    app_settings.jwt_current_kid = original_kid
    try:
        with pytest.raises(Exception) as exc_info:
            security.decode_token(token)
        assert getattr(exc_info.value, "status_code", None) == 401
    finally:
        app_settings.jwt_signing_keys = original_keys
        app_settings.jwt_current_kid = original_kid
