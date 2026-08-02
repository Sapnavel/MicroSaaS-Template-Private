"""Tests for `app.services.notification_engine` (PRPs/notification-hub-prp.md,
Phase 3). Runs against a real Postgres (see tests/conftest.py's module
docstring), reusing fixtures from prior modules' TEST-AGENTs (`db`, `branch`,
`patient`, `doctor_record`, `room`, `in_progress_appointment`).

This file calls `notification_engine.handle_event(db, topic=..., payload=...)`
directly -- no RabbitMQ/pika involved. `handle_event` is the consumer's pure,
unit-testable entry point (see that module's docstring); the actual `pika`
consume loop (`workers/notification_consumer.py`) is a thin wrapper around it
and is reviewed statically rather than exercised end-to-end (a live-broker
test was judged too flaky/slow a substitute for exercising the exact same
logic this file already calls directly -- see the PRP's Phase 3 plan, which
explicitly allows this as a substitute).

Payload shapes below are copied byte-for-byte from the actual
`event_publisher.publish(...)` call sites (not guessed):
- `services/scheduling_engine.py` `book_appointment` -> `appointment.booked`
  (appointment_id, branch_id, doctor_id, patient_id, start_time, is_emergency)
- `services/scheduling_engine.py` `cancel_appointment` -> `appointment.cancelled`
  (appointment_id, reason)
- `services/scheduling_engine.py` `recalculate_downstream_wait_times` ->
  `queue.wait_time_updated` (appointment_id, doctor_id)
- `services/emergency_engine.py` (preempt path) -> `appointment.preempted`
  (victim_appointment_id, victim_patient_id, preempted_by_appointment_id,
  doctor_id, triage_level)
"""

import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.notification import Notification
from app.services import notification_engine
from app.services.notification_engine import handle_event


# ---------------------------------------------------------------------------
# 1. Each of the four recognized topics: correct recipient resolution
#    (patient_id, straight-off-payload vs via Appointment lookup),
#    correct channel/template/branch_id, per the handlers' own docstrings.
# ---------------------------------------------------------------------------


def test_appointment_booked_resolves_patient_and_branch_straight_off_payload(db, branch, patient):
    """`_handle_appointment_booked`: `scheduling_engine.book_appointment` /
    `emergency_engine`'s emergency path both publish `patient_id`/`branch_id`
    directly -- no `Appointment` lookup needed, so a random `appointment_id`
    (not backed by a real row) is fine here."""
    payload = {
        "appointment_id": str(uuid.uuid4()),
        "branch_id": str(branch.id),
        "doctor_id": str(uuid.uuid4()),
        "patient_id": str(patient.id),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "is_emergency": False,
    }

    notification = handle_event(db, topic="appointment.booked", payload=payload)

    assert notification is not None
    assert notification.channel == "sms"
    assert notification.template == "appointment_confirmation"
    assert notification.patient_id == patient.id
    assert notification.branch_id == branch.id
    assert notification.user_id is None
    assert notification.payload == payload
    assert notification.status == "sent"  # LoggingNotificationProvider always succeeds

    # Row genuinely persisted, not just returned in-memory.
    db.expire_all()
    reloaded = db.get(Notification, notification.id)
    assert reloaded is not None
    assert reloaded.status == "sent"


def test_appointment_cancelled_resolves_via_appointment_lookup(db, in_progress_appointment):
    """`_handle_appointment_cancelled`: `scheduling_engine.cancel_appointment`
    only publishes `appointment_id`/`reason` -- `patient_id`/`branch_id` must
    come from a real `Appointment` lookup, not the payload."""
    payload = {"appointment_id": str(in_progress_appointment.id), "reason": "patient request"}

    notification = handle_event(db, topic="appointment.cancelled", payload=payload)

    assert notification is not None
    assert notification.channel == "sms"
    assert notification.template == "appointment_cancelled"
    assert notification.patient_id == in_progress_appointment.patient_id
    assert notification.branch_id == in_progress_appointment.branch_id
    assert notification.user_id is None
    assert notification.status == "sent"


def test_appointment_preempted_resolves_victim_patient_and_branch_via_lookup(db, in_progress_appointment):
    """`_handle_appointment_preempted`: `victim_patient_id` is used as-is
    (straight off the payload) for `patient_id`, but `branch_id` still comes
    from an `Appointment` lookup on `victim_appointment_id` (the payload
    itself carries no `branch_id`)."""
    payload = {
        "victim_appointment_id": str(in_progress_appointment.id),
        "victim_patient_id": str(in_progress_appointment.patient_id),
        "preempted_by_appointment_id": str(uuid.uuid4()),
        "doctor_id": str(in_progress_appointment.doctor_id),
        "triage_level": "1",
    }

    notification = handle_event(db, topic="appointment.preempted", payload=payload)

    assert notification is not None
    assert notification.channel == "sms"
    assert notification.template == "appointment_preempted_rebooking_offer"
    assert notification.patient_id == in_progress_appointment.patient_id
    assert notification.branch_id == in_progress_appointment.branch_id
    assert notification.user_id is None
    assert notification.status == "sent"


def test_queue_wait_time_updated_resolves_via_appointment_lookup(db, in_progress_appointment):
    """`_handle_wait_time_updated`: only `appointment_id`/`doctor_id` are
    published -- `patient_id`/`branch_id` come from the `Appointment` lookup.
    Channel is `push` (not `sms`, unlike the other three)."""
    payload = {
        "appointment_id": str(in_progress_appointment.id),
        "doctor_id": str(in_progress_appointment.doctor_id),
    }

    notification = handle_event(db, topic="queue.wait_time_updated", payload=payload)

    assert notification is not None
    assert notification.channel == "push"
    assert notification.template == "wait_time_update"
    assert notification.patient_id == in_progress_appointment.patient_id
    assert notification.branch_id == in_progress_appointment.branch_id
    assert notification.user_id is None
    assert notification.status == "sent"


# COVERAGE GAP (noted, not invented around): every one of the four current
# `_TOPIC_HANDLERS` entries resolves `patient_id` and leaves `user_id` unset
# (see each handler above -- `user_id=None` in every `_ResolvedRecipient`).
# `_resolve_recipient_string`'s `User.email` branch and
# `Notification.user_id` are therefore only exercised by
# `notification_service.retry_notification`'s user_id branch (see
# test_notifications_router.py) and by direct construction in this file's
# router-filter tests -- no currently-published topic reaches the
# `user_id`/`User.email` path inside `notification_engine.py` itself. This
# is a real gap in this PRP's four-topic scope, not a test omission: there is
# no topic to exercise it with without inventing one that doesn't exist in
# the codebase (out of scope per the PRP's own SCOPE-DEFINING FACT).


# ---------------------------------------------------------------------------
# 2. Unrecognized topic: None, no row created, warning logged.
# ---------------------------------------------------------------------------


def test_unrecognized_topic_returns_none_and_creates_no_row(db, caplog):
    before_count = db.execute(select(Notification)).scalars().all()
    assert before_count == []

    with caplog.at_level(logging.WARNING, logger="app.services.notification_engine"):
        result = handle_event(db, topic="lab.result_ready", payload={"lab_order_id": str(uuid.uuid4())})

    assert result is None
    assert "lab.result_ready" in caplog.text
    after_count = db.execute(select(Notification)).scalars().all()
    assert after_count == []


# ---------------------------------------------------------------------------
# 3. Provider failure (False return, or a raised exception): status="failed",
#    handle_event never raises, and the row still persists (created before
#    the provider is ever called).
# ---------------------------------------------------------------------------


class _FalseProvider:
    def send(self, *, channel, recipient, template, payload):
        return False


class _RaisingProvider:
    def send(self, *, channel, recipient, template, payload):
        raise RuntimeError("simulated provider outage")


def test_provider_returning_false_results_in_failed_status(db, branch, patient, monkeypatch):
    monkeypatch.setattr(notification_engine, "get_provider", lambda channel: _FalseProvider())

    payload = {
        "appointment_id": str(uuid.uuid4()),
        "branch_id": str(branch.id),
        "doctor_id": str(uuid.uuid4()),
        "patient_id": str(patient.id),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "is_emergency": False,
    }

    notification = handle_event(db, topic="appointment.booked", payload=payload)

    assert notification is not None
    assert notification.status == "failed"

    db.expire_all()
    reloaded = db.get(Notification, notification.id)
    assert reloaded.status == "failed"


def test_provider_raising_is_swallowed_and_results_in_failed_status(db, branch, patient, monkeypatch):
    """Per `handle_event`'s own docstring: ANY exception raised by the
    provider must never propagate -- a single bad/failing send must never
    crash the consumer loop. `handle_event` itself must not raise here."""
    monkeypatch.setattr(notification_engine, "get_provider", lambda channel: _RaisingProvider())

    payload = {
        "appointment_id": str(uuid.uuid4()),
        "branch_id": str(branch.id),
        "doctor_id": str(uuid.uuid4()),
        "patient_id": str(patient.id),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "is_emergency": False,
    }

    notification = handle_event(db, topic="appointment.booked", payload=payload)

    assert notification is not None
    assert notification.status == "failed"

    db.expire_all()
    reloaded = db.get(Notification, notification.id)
    assert reloaded is not None
    assert reloaded.status == "failed"


# ---------------------------------------------------------------------------
# 4. A referenced appointment_id that doesn't exist raises ValueError --
#    propagates OUT of handle_event (the consumer worker's job to catch, not
#    handle_event's, per `_lookup_appointment`'s docstring).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic,payload_factory",
    [
        ("appointment.cancelled", lambda missing_id: {"appointment_id": missing_id, "reason": "x"}),
        (
            "queue.wait_time_updated",
            lambda missing_id: {"appointment_id": missing_id, "doctor_id": str(uuid.uuid4())},
        ),
        (
            "appointment.preempted",
            lambda missing_id: {
                "victim_appointment_id": missing_id,
                "victim_patient_id": str(uuid.uuid4()),
                "preempted_by_appointment_id": str(uuid.uuid4()),
                "doctor_id": str(uuid.uuid4()),
                "triage_level": "1",
            },
        ),
    ],
)
def test_missing_appointment_raises_value_error_and_propagates(db, topic, payload_factory):
    missing_id = str(uuid.uuid4())
    payload = payload_factory(missing_id)

    with pytest.raises(ValueError):
        handle_event(db, topic=topic, payload=payload)

    # No Notification row should have been created -- the lookup failure
    # happens inside the topic handler, before `handle_event` ever
    # constructs/adds a `Notification`.
    rows = db.execute(select(Notification)).scalars().all()
    assert rows == []
