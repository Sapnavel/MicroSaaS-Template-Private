"""Service-layer tests for the Real-Time Queue & Digital Token module
(services/queue_service.py, workers/queue_wait_time_consumer.py), per
PRPs/realtime-queue-prp.md Phase 3. Runs against a real Postgres + Redis (see
tests/conftest.py's module docstring), reusing fixtures from prior modules'
TEST-AGENTs (`db`, `branch`, `other_branch`, `patient`, `in_progress_appointment`,
`doctor_record`, `room`, `specialty`, `front_desk_user`, `nurse_user`,
`system_admin_user`, `staff_user` (doctor, see conftest.py's docstring),
`pharmacist_user`, `billing_admin_user`).

`check_in`/`update_status` are `async def` (they broadcast over the live
WebSocket channel, `app.websocket.queue_board.manager`, whose `broadcast`
iterating an empty connection set -- no test client ever subscribes -- is a
safe no-op, confirmed by reading `app/websocket/queue_board.py`). Neither
`pytest-asyncio` nor any equivalent is a dependency of this backend
(confirmed: absent from backend/requirements.txt, and no existing test in
this suite calls an async function directly), so these are called from plain
sync test functions via `asyncio.run(...)` -- there is no already-running
event loop in that context, so this is safe (unlike the router's own
HTTP-level async endpoints, which FastAPI's TestClient drives through its own
internal event loop machinery -- see test_queue_router.py).
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.queue import QueueToken, TokenStatus
from app.services import queue_service
from app.services.queue_service import (
    AppointmentNotFoundError,
    IllegalTokenStatusTransition,
    QueueCheckInPayload,
    QueueTokenNotFoundError,
)
from app.workers.queue_wait_time_consumer import (
    _handle_wait_time_updated,
    _recompute_estimated_wait_minutes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_token(
    db,
    *,
    branch_id,
    department_id=None,
    appointment_id=None,
    status=TokenStatus.waiting,
    token_number=1,
    checked_in_at=None,
    estimated_wait_minutes=None,
    is_priority=False,
) -> QueueToken:
    token = QueueToken(
        branch_id=branch_id,
        department_id=department_id,
        appointment_id=appointment_id,
        status=status,
        token_number=token_number,
        estimated_wait_minutes=estimated_wait_minutes,
        is_priority=is_priority,
    )
    if checked_in_at is not None:
        token.checked_in_at = checked_in_at
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def _forbidden(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 403


# ---------------------------------------------------------------------------
# 1. check_in -- walk-in path.
# ---------------------------------------------------------------------------


def test_check_in_walk_in_creates_waiting_token_token_number_1(
    db, branch, specialty, front_desk_user
):
    token = _run(
        queue_service.check_in(
            db,
            front_desk_user,
            QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id),
        )
    )

    assert token.status == TokenStatus.waiting
    assert token.appointment_id is None
    assert token.branch_id == branch.id
    assert token.department_id == specialty.id
    assert token.token_number == 1


def test_check_in_walk_in_token_number_increments_same_day_same_branch_department(
    db, branch, specialty, front_desk_user
):
    first = _run(
        queue_service.check_in(
            db, front_desk_user, QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id)
        )
    )
    second = _run(
        queue_service.check_in(
            db, front_desk_user, QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id)
        )
    )

    assert first.token_number == 1
    assert second.token_number == 2


# ---------------------------------------------------------------------------
# 2. check_in -- appointment-linked path: branch_id/department_id DERIVED,
#    never taken from a caller-supplied value (none is supplied here at all).
# ---------------------------------------------------------------------------


def test_check_in_appointment_linked_derives_branch_and_department(
    db, in_progress_appointment, doctor_record, front_desk_user
):
    token = _run(
        queue_service.check_in(
            db,
            front_desk_user,
            QueueCheckInPayload(appointment_id=in_progress_appointment.id),
        )
    )

    assert token.appointment_id == in_progress_appointment.id
    assert token.branch_id == in_progress_appointment.branch_id
    assert token.department_id == doctor_record.specialty_id
    assert token.status == TokenStatus.waiting
    assert token.token_number == 1


def test_check_in_walk_in_priority_flag_honored(db, branch, specialty, front_desk_user):
    """HMS Project Completion Prompt gap ("emergency queue priority")."""
    token = _run(
        queue_service.check_in(
            db,
            front_desk_user,
            QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id, is_priority=True),
        )
    )
    assert token.is_priority is True


def test_check_in_walk_in_defaults_to_not_priority(db, branch, specialty, front_desk_user):
    token = _run(
        queue_service.check_in(
            db, front_desk_user, QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id)
        )
    )
    assert token.is_priority is False


def test_check_in_appointment_linked_inherits_emergency_flag(
    db, in_progress_appointment, front_desk_user
):
    """A caller doesn't have to remember to re-flag priority for an
    appointment already booked as an emergency (`is_emergency=True` set by
    `emergency_engine.py` at booking time) -- `check_in` inherits it."""
    in_progress_appointment.is_emergency = True
    db.add(in_progress_appointment)
    db.commit()

    token = _run(
        queue_service.check_in(
            db, front_desk_user, QueueCheckInPayload(appointment_id=in_progress_appointment.id)
        )
    )
    assert token.is_priority is True


def test_check_in_appointment_linked_explicit_override_adds_priority(
    db, in_progress_appointment, front_desk_user
):
    """A non-emergency-booked appointment can still be marked priority
    explicitly at check-in (e.g. the patient's condition worsened after
    arrival) -- OR-ed, never overridden away."""
    assert in_progress_appointment.is_emergency is False

    token = _run(
        queue_service.check_in(
            db,
            front_desk_user,
            QueueCheckInPayload(appointment_id=in_progress_appointment.id, is_priority=True),
        )
    )
    assert token.is_priority is True


def test_check_in_nonexistent_appointment_raises(db, front_desk_user):
    with pytest.raises(AppointmentNotFoundError):
        _run(
            queue_service.check_in(
                db, front_desk_user, QueueCheckInPayload(appointment_id=uuid.uuid4())
            )
        )


# ---------------------------------------------------------------------------
# 3. check_in / update_status / list_queue RBAC.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_fixture", ["front_desk_user", "nurse_user", "system_admin_user"])
def test_check_in_allowed_for_write_roles(db, request, role_fixture, branch, specialty):
    user = request.getfixturevalue(role_fixture)
    token = _run(
        queue_service.check_in(
            db, user, QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id)
        )
    )
    assert token.status == TokenStatus.waiting


@pytest.mark.parametrize("role_fixture", ["staff_user", "pharmacist_user", "billing_admin_user"])
def test_check_in_denied_for_non_write_roles(db, request, role_fixture, branch, specialty):
    """`staff_user` is role=doctor (see conftest.py's docstring) -- read-only
    per design decision #6. `pharmacist_user`/`billing_admin_user` have no
    queue_token policy registered at all -- denied by authorize()'s
    default-deny."""
    user = request.getfixturevalue(role_fixture)
    with pytest.raises(Exception) as exc_info:
        _run(
            queue_service.check_in(
                db, user, QueueCheckInPayload(branch_id=branch.id, department_id=specialty.id)
            )
        )
    assert _forbidden(exc_info.value)


@pytest.mark.parametrize("role_fixture", ["front_desk_user", "nurse_user", "system_admin_user"])
def test_update_status_allowed_for_write_roles(db, request, role_fixture, branch):
    user = request.getfixturevalue(role_fixture)
    token = _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)

    updated = _run(queue_service.update_status(db, user, token.id, TokenStatus.in_consultation))

    assert updated.status == TokenStatus.in_consultation
    assert updated.called_at is not None


@pytest.mark.parametrize("role_fixture", ["staff_user", "pharmacist_user", "billing_admin_user"])
def test_update_status_denied_for_non_write_roles(db, request, role_fixture, branch):
    user = request.getfixturevalue(role_fixture)
    token = _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)

    with pytest.raises(Exception) as exc_info:
        _run(queue_service.update_status(db, user, token.id, TokenStatus.in_consultation))
    assert _forbidden(exc_info.value)


@pytest.mark.parametrize(
    "role_fixture", ["front_desk_user", "nurse_user", "system_admin_user", "staff_user"]
)
def test_list_queue_allowed_for_read_roles_including_doctor(db, request, role_fixture, branch):
    """`staff_user` is role=doctor -- read-only per design decision #6, so it
    IS allowed here even though it is denied on check_in/update_status
    above."""
    user = request.getfixturevalue(role_fixture)
    _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)

    tokens = queue_service.list_queue(db, user, branch.id, None)

    assert len(tokens) == 1


@pytest.mark.parametrize("role_fixture", ["pharmacist_user", "billing_admin_user"])
def test_list_queue_denied_for_roles_with_no_queue_token_policy(db, request, role_fixture, branch):
    user = request.getfixturevalue(role_fixture)

    with pytest.raises(Exception) as exc_info:
        queue_service.list_queue(db, user, branch.id, None)
    assert _forbidden(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Cross-branch tenant guard.
# ---------------------------------------------------------------------------


def test_check_in_cross_branch_denied(db, front_desk_user, other_branch, specialty):
    """front_desk_user is scoped to `branch` -- checking a walk-in in at
    `other_branch` must be denied by authorize()'s tenant guard."""
    with pytest.raises(Exception) as exc_info:
        _run(
            queue_service.check_in(
                db,
                front_desk_user,
                QueueCheckInPayload(branch_id=other_branch.id, department_id=specialty.id),
            )
        )
    assert _forbidden(exc_info.value)


def test_update_status_cross_branch_denied(db, front_desk_user, other_branch):
    """A token belonging to `other_branch` cannot be transitioned by a caller
    scoped to `branch` -- mirrors test_billing_router.py's/
    test_notifications_router.py's cross-branch pattern."""
    other_branch_token = _make_token(db, branch_id=other_branch.id, status=TokenStatus.waiting)

    with pytest.raises(Exception) as exc_info:
        _run(queue_service.update_status(db, front_desk_user, other_branch_token.id, TokenStatus.in_consultation))
    assert _forbidden(exc_info.value)


def test_list_queue_cross_branch_denied(db, front_desk_user, other_branch):
    """front_desk_user (scoped to `branch`) requesting the board for
    `other_branch` is denied by the `_BranchScoped` tenant guard."""
    with pytest.raises(Exception) as exc_info:
        queue_service.list_queue(db, front_desk_user, other_branch.id, None)
    assert _forbidden(exc_info.value)


def test_list_queue_excludes_other_branch_tokens(db, front_desk_user, system_admin_user, branch, other_branch):
    own = _make_token(db, branch_id=branch.id, status=TokenStatus.waiting)
    _make_token(db, branch_id=other_branch.id, status=TokenStatus.waiting)

    tokens = queue_service.list_queue(db, front_desk_user, branch.id, None)

    ids = {t.id for t in tokens}
    assert ids == {own.id}


# ---------------------------------------------------------------------------
# 5. list_queue filtering + ordering.
# ---------------------------------------------------------------------------


def test_list_queue_filters_by_department_id_and_orders_by_checked_in_at(
    db, system_admin_user, branch, specialty
):
    now = datetime.now(timezone.utc)
    other_dept_token = _make_token(
        db, branch_id=branch.id, department_id=None, checked_in_at=now - timedelta(minutes=1)
    )
    earliest = _make_token(
        db, branch_id=branch.id, department_id=specialty.id, checked_in_at=now - timedelta(minutes=30)
    )
    latest = _make_token(
        db, branch_id=branch.id, department_id=specialty.id, checked_in_at=now - timedelta(minutes=5)
    )

    all_tokens = queue_service.list_queue(db, system_admin_user, branch.id, None)
    assert {t.id for t in all_tokens} == {other_dept_token.id, earliest.id, latest.id}

    dept_tokens = queue_service.list_queue(db, system_admin_user, branch.id, specialty.id)
    assert [t.id for t in dept_tokens] == [earliest.id, latest.id]


def test_list_queue_priority_tokens_sort_first_but_fairly_among_themselves(
    db, system_admin_user, branch, specialty
):
    """HMS Project Completion Prompt gap ("emergency queue priority"):
    `is_priority DESC, checked_in_at ASC` -- BOTH priority tokens sort ahead
    of BOTH non-priority tokens regardless of arrival time, but the two
    priority tokens (and separately the two non-priority tokens) keep their
    own fair earliest-arrived-first order relative to each other."""
    now = datetime.now(timezone.utc)
    non_priority_early = _make_token(
        db, branch_id=branch.id, department_id=specialty.id, checked_in_at=now - timedelta(minutes=60)
    )
    priority_late = _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=10),
        is_priority=True,
    )
    priority_early = _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=30),
        is_priority=True,
    )
    non_priority_late = _make_token(
        db, branch_id=branch.id, department_id=specialty.id, checked_in_at=now - timedelta(minutes=5)
    )

    tokens = queue_service.list_queue(db, system_admin_user, branch.id, specialty.id)

    assert [t.id for t in tokens] == [
        priority_early.id,
        priority_late.id,
        non_priority_early.id,
        non_priority_late.id,
    ]


# ---------------------------------------------------------------------------
# 6. Status-transition state machine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target,expect_called_at,expect_completed_at",
    [
        (TokenStatus.waiting, TokenStatus.in_consultation, True, False),
        (TokenStatus.waiting, TokenStatus.delayed, False, False),
        (TokenStatus.waiting, TokenStatus.skipped, False, False),
        (TokenStatus.delayed, TokenStatus.waiting, False, False),
        (TokenStatus.delayed, TokenStatus.in_consultation, True, False),
        (TokenStatus.delayed, TokenStatus.skipped, False, False),
        (TokenStatus.skipped, TokenStatus.waiting, False, False),
        (TokenStatus.in_consultation, TokenStatus.done, False, True),
    ],
)
def test_legal_transitions_succeed_and_stamp_timestamps(
    db, front_desk_user, branch, current, target, expect_called_at, expect_completed_at
):
    token = _make_token(db, branch_id=branch.id, status=current)

    updated = _run(queue_service.update_status(db, front_desk_user, token.id, target))

    assert updated.status == target
    assert (updated.called_at is not None) == expect_called_at
    assert (updated.completed_at is not None) == expect_completed_at


@pytest.mark.parametrize(
    "current,target",
    [
        (TokenStatus.done, TokenStatus.waiting),
        (TokenStatus.waiting, TokenStatus.done),
        (TokenStatus.skipped, TokenStatus.in_consultation),
    ],
)
def test_illegal_transitions_raise(db, front_desk_user, branch, current, target):
    token = _make_token(db, branch_id=branch.id, status=current)

    with pytest.raises(IllegalTokenStatusTransition):
        _run(queue_service.update_status(db, front_desk_user, token.id, target))


def test_update_status_nonexistent_token_raises(db, front_desk_user):
    with pytest.raises(QueueTokenNotFoundError):
        _run(queue_service.update_status(db, front_desk_user, uuid.uuid4(), TokenStatus.in_consultation))


# ---------------------------------------------------------------------------
# 7. queue_wait_time_consumer -- pure-logic recompute functions.
# ---------------------------------------------------------------------------


def test_recompute_estimated_wait_minutes_counts_only_waiting_and_delayed_ahead(
    db, branch, specialty
):
    now = datetime.now(timezone.utc)
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        status=TokenStatus.waiting,
        checked_in_at=now - timedelta(minutes=30),
    )
    # An in_consultation token ahead of the target should NOT count -- only
    # waiting/delayed tokens are "still ahead in line" for the heuristic.
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        status=TokenStatus.in_consultation,
        checked_in_at=now - timedelta(minutes=20),
    )
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        status=TokenStatus.delayed,
        checked_in_at=now - timedelta(minutes=10),
    )
    target = _make_token(
        db, branch_id=branch.id, department_id=specialty.id, status=TokenStatus.waiting, checked_in_at=now
    )

    result = _recompute_estimated_wait_minutes(db, target)

    assert result == 2 * queue_service.AVERAGE_CONSULTATION_MINUTES  # 30


def test_handle_wait_time_updated_no_token_for_appointment_is_a_no_op(db):
    # Should not raise -- "no token checked in yet" is logged and skipped.
    _handle_wait_time_updated(db, {"appointment_id": str(uuid.uuid4())})


@pytest.mark.parametrize("status", [TokenStatus.in_consultation, TokenStatus.done, TokenStatus.skipped])
def test_handle_wait_time_updated_skips_non_recomputable_statuses(
    db, in_progress_appointment, branch, specialty, status
):
    token = _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        appointment_id=in_progress_appointment.id,
        status=status,
        estimated_wait_minutes=999,
    )

    _handle_wait_time_updated(db, {"appointment_id": str(in_progress_appointment.id)})

    db.expire_all()
    reloaded = db.get(QueueToken, token.id)
    assert reloaded.estimated_wait_minutes == 999  # left unchanged


def test_handle_wait_time_updated_recomputes_for_waiting_token(
    db, in_progress_appointment, branch, specialty
):
    now = datetime.now(timezone.utc)
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        status=TokenStatus.waiting,
        checked_in_at=now - timedelta(minutes=10),
    )
    target = _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        appointment_id=in_progress_appointment.id,
        status=TokenStatus.waiting,
        checked_in_at=now,
        estimated_wait_minutes=None,
    )

    _handle_wait_time_updated(db, {"appointment_id": str(in_progress_appointment.id)})

    db.expire_all()
    reloaded = db.get(QueueToken, target.id)
    assert reloaded.estimated_wait_minutes == 1 * queue_service.AVERAGE_CONSULTATION_MINUTES
