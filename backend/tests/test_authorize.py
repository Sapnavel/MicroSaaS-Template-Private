"""Unit tests for core/security.py's ABAC `authorize()` tenant guard + policy
registry, per PRPs/auth-rbac-abac-prp.md Phase 3.

There is no real protected-resource endpoint yet to exercise these through
(every consumer router besides auth/admin is still a stub), so per the task
brief these call `authorize()` directly against bare objects that only carry
the `.branch_id` attribute the tenant guard inspects. `authorize()` never
touches the DB, so this needs no Postgres/Redis fixtures -- plain pytest.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.security import authorize
from app.models.user import UserRole


def make_user(role: UserRole, branch_id: str | None):
    return SimpleNamespace(id="test-user-id", role=role, branch_id=branch_id)


def make_resource(branch_id: str | None):
    return SimpleNamespace(branch_id=branch_id)


def test_same_branch_resource_with_registered_policy_is_allowed():
    """front_desk has a registered ("front_desk", "patient", "read") policy
    that always returns True; same branch_id on caller and resource should
    pass both the tenant guard and the policy check."""
    user = make_user(UserRole.front_desk, "branch-a")
    resource = make_resource("branch-a")

    authorize(user, "patient", "read", resource)  # must not raise


def test_cross_branch_resource_denied_even_with_matching_policy():
    """Tenant guard runs BEFORE the registered policy, so a resource in a
    different branch is denied even though (front_desk, patient, read) has
    a policy registered that would otherwise return True."""
    user = make_user(UserRole.front_desk, "branch-a")
    resource = make_resource("branch-b")

    with pytest.raises(HTTPException) as exc_info:
        authorize(user, "patient", "read", resource)
    assert exc_info.value.status_code == 403


def test_system_admin_bypasses_tenant_guard_cross_branch():
    """system_admin is exempt from the tenant guard entirely, and has a
    (system_admin, "*", "*") wildcard policy registered, so a cross-branch
    resource is still allowed."""
    user = make_user(UserRole.system_admin, "branch-a")
    resource = make_resource("branch-b")

    authorize(user, "patient", "read", resource)  # must not raise


def test_no_registered_policy_denies_by_default():
    """(role, resource_type, action) tuples with no registered policy (and
    no matching wildcard) must be denied, not silently allowed -- lab_tech
    has no policies registered at all in core/security.py."""
    user = make_user(UserRole.lab_tech, "branch-a")
    resource = make_resource("branch-a")  # same branch, so the tenant guard passes

    with pytest.raises(HTTPException) as exc_info:
        authorize(user, "some_unregistered_resource_type", "some_action", resource)
    assert exc_info.value.status_code == 403


def test_resource_without_branch_id_skips_tenant_guard():
    """Resources with no `branch_id` attribute at all (e.g. a global,
    non-tenant-scoped resource) skip the tenant guard and fall straight to
    the policy lookup."""
    user = make_user(UserRole.system_admin, "branch-a")
    resource = SimpleNamespace(name="global-resource")  # no branch_id attribute

    authorize(user, "patient", "read", resource)  # system_admin wildcard still allows it
