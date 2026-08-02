"""Event -> Notification consumer engine (PRPs/notification-hub-prp.md
Phase 1).

`handle_event` is the single entry point, deliberately kept as a plain,
unit-testable function with no broker dependency of its own — the real
`pika` `BlockingConnection`/`basic_consume` loop (Phase 2's
`workers/notification_consumer.py`) is a thin wrapper around it: decode
JSON off the wire, call `handle_event`, ack on success, log + nack
(`requeue=False`, no DLQ exists — see the PRP's design decision #2) on any
exception. This mirrors why `ward_engine.py` splits its pure logic from its
locking/transaction shell: keep the part worth unit-testing free of the
part that needs a real broker connection to exercise at all.

SCOPE-DEFINING FACT (read before adding a fifth entry casually): as of this
PRP, exactly **four** event topics are ever actually published anywhere in
this codebase — `appointment.booked`/`appointment.cancelled` and
`queue.wait_time_updated` from `services/scheduling_engine.py`, and
`appointment.booked`/`appointment.preempted` from
`services/emergency_engine.py`. Lab, Pharmacy, Ward, and Billing (all four
already built and shipped) never call `event_publisher.publish(...)`
anywhere — retrofitting that into four already-reviewed, already-settled
modules is explicitly out of scope for this PRP (see the PRP's module
overview for the full reasoning). `_TOPIC_HANDLERS` is therefore built
generically enough that a fifth module publishing a fifth topic later is a
one-entry registry addition, not an architecture change — but that entry
does not exist yet, on purpose, and an unrecognized topic reaching
`handle_event` is NOT an error: it's logged at `warning` and `None` is
returned, because there is deliberately nothing wired up for it yet.
"""

import logging
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.notification_providers import get_provider
from app.models.appointment import Appointment
from app.models.notification import Notification, NotificationStatus
from app.models.patient import Patient
from app.models.user import User

logger = logging.getLogger(__name__)


@dataclass
class _ResolvedRecipient:
    """What a `_TOPIC_HANDLERS` entry resolves from a raw payload dict,
    before `handle_event` turns it into an actual `Notification` row.
    `user_id`/`patient_id` are independent and (for all four topics
    currently wired) mutually exclusive in practice — exactly one is set,
    matching `Notification`'s own "never both" convention (see that
    model's docstring)."""

    user_id: UUID | None
    patient_id: UUID | None
    branch_id: UUID | None
    channel: str
    template: str


def _uuid(value: str) -> UUID:
    """Payloads arrive as plain JSON (decoded off the RabbitMQ wire, or
    handed in directly by a test) — every ID in them is a `str(...)` of the
    original UUID (see `core/events.py`'s `RabbitMQPublisher.publish`,
    which json-dumps with `default=str`), never a `UUID` instance."""
    return UUID(value)


def _lookup_appointment(db: Session, appointment_id: str) -> Appointment:
    """Resolve an `Appointment` row for the three topics whose payload
    doesn't carry `patient_id`/`branch_id` directly (per the PRP's design
    decision #4). Raises `ValueError` if the id doesn't resolve — this is a
    genuine data-integrity problem (the event references an appointment
    that doesn't exist), deliberately NOT swallowed here the way a provider
    failure is: `workers/notification_consumer.py`'s outer loop is what
    catches this, logs it, and nacks the message (no DLQ to requeue into)."""
    appointment = db.get(Appointment, _uuid(appointment_id))
    if appointment is None:
        raise ValueError(f"appointment {appointment_id!r} not found while resolving a notification")
    return appointment


def _handle_appointment_booked(db: Session, payload: dict) -> _ResolvedRecipient:
    """`scheduling_engine.book_appointment` / `emergency_engine.preempt_and_book`
    both publish `patient_id`/`branch_id` directly on this topic — no
    `Appointment` lookup needed."""
    return _ResolvedRecipient(
        user_id=None,
        patient_id=_uuid(payload["patient_id"]),
        branch_id=_uuid(payload["branch_id"]),
        channel="sms",
        template="appointment_confirmation",
    )


def _handle_appointment_cancelled(db: Session, payload: dict) -> _ResolvedRecipient:
    """`scheduling_engine.cancel_appointment` only publishes `appointment_id`/
    `reason` — `patient_id`/`branch_id` must come from an `Appointment`
    lookup."""
    appointment = _lookup_appointment(db, payload["appointment_id"])
    return _ResolvedRecipient(
        user_id=None,
        patient_id=appointment.patient_id,
        branch_id=appointment.branch_id,
        channel="sms",
        template="appointment_cancelled",
    )


def _handle_appointment_preempted(db: Session, payload: dict) -> _ResolvedRecipient:
    """`emergency_engine.preempt_and_book` publishes `victim_patient_id`
    directly (used as-is for `patient_id`) but no `branch_id` — resolved via
    an `Appointment` lookup on `victim_appointment_id`. Template name per
    `docs/ARCHITECTURE.md` §6 point 5's own wording for this exact
    "stubbed" consumer."""
    appointment = _lookup_appointment(db, payload["victim_appointment_id"])
    return _ResolvedRecipient(
        user_id=None,
        patient_id=_uuid(payload["victim_patient_id"]),
        branch_id=appointment.branch_id,
        channel="sms",
        template="appointment_preempted_rebooking_offer",
    )


def _handle_wait_time_updated(db: Session, payload: dict) -> _ResolvedRecipient:
    """`scheduling_engine.recalculate_downstream_wait_times` only publishes
    `appointment_id`/`doctor_id` — `patient_id`/`branch_id` come from an
    `Appointment` lookup."""
    appointment = _lookup_appointment(db, payload["appointment_id"])
    return _ResolvedRecipient(
        user_id=None,
        patient_id=appointment.patient_id,
        branch_id=appointment.branch_id,
        channel="push",
        template="wait_time_update",
    )


_TOPIC_HANDLERS: dict[str, Callable[[Session, dict], _ResolvedRecipient]] = {
    "appointment.booked": _handle_appointment_booked,
    "appointment.cancelled": _handle_appointment_cancelled,
    "appointment.preempted": _handle_appointment_preempted,
    "queue.wait_time_updated": _handle_wait_time_updated,
}


def _resolve_recipient_string(db: Session, resolved: _ResolvedRecipient) -> str | None:
    """The human-meaningful string a provider would actually use (a phone
    number for `sms`/`push`'s stub, an email address for `email`) —
    resolved here, not inside the provider (it has no DB access and
    doesn't know how to look up a patient/user). `Patient.phone` is an
    `EncryptedString` column that decrypts transparently on this read."""
    if resolved.patient_id is not None:
        patient = db.get(Patient, resolved.patient_id)
        return patient.phone if patient is not None else None
    if resolved.user_id is not None:
        user = db.get(User, resolved.user_id)
        return user.email if user is not None else None
    return None


def handle_event(db: Session, *, topic: str, payload: dict) -> Notification | None:
    """The consumer's single entry point.

    Returns `None` (logging a warning, not an error) for a topic with no
    registered handler — see the module docstring's scope note.

    For a recognized topic: resolves the recipient via `_TOPIC_HANDLERS`,
    resolves the actual recipient string, creates the `Notification` row as
    `status="queued"` and commits immediately (so the row exists even if
    the send below fails), then calls the channel's provider. ANY
    exception raised by the provider (or a plain `False` return) results in
    `status="failed"` — never left propagating, since a single bad/failing
    send must never crash the caller (a background consumer processing one
    message at a time can't afford to die on one failure). Commits again
    either way and returns the row.
    """
    handler = _TOPIC_HANDLERS.get(topic)
    if handler is None:
        logger.warning("no notification handler registered for topic=%s; ignoring event", topic)
        return None

    resolved = handler(db, payload)
    recipient = _resolve_recipient_string(db, resolved)
    if recipient is None:
        logger.warning(
            "could not resolve a recipient string for topic=%s user_id=%s patient_id=%s; sending empty recipient",
            topic,
            resolved.user_id,
            resolved.patient_id,
        )

    notification = Notification(
        user_id=resolved.user_id,
        patient_id=resolved.patient_id,
        branch_id=resolved.branch_id,
        channel=resolved.channel,
        template=resolved.template,
        payload=payload,
        status=NotificationStatus.queued.value,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)

    if recipient is None:
        # No phone/email to send to at all -- never a "successful send",
        # regardless of what a stub provider would otherwise return.
        succeeded = False
    else:
        try:
            succeeded = get_provider(resolved.channel).send(
                channel=resolved.channel,
                recipient=recipient,
                template=resolved.template,
                payload=payload,
            )
        except Exception:
            logger.exception(
                "notification provider raised for notification_id=%s channel=%s template=%s",
                notification.id,
                resolved.channel,
                resolved.template,
            )
            succeeded = False

    notification.status = NotificationStatus.sent.value if succeeded else NotificationStatus.failed.value
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification
