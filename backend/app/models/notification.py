"""ORM model for the Notification & Alert Hub module (database/schema.sql
§12: `notifications`), PRPs/notification-hub-prp.md Phase 1.

This module is architecturally different from every other module in this
codebase: it is not a request/response engine, it's a consumer that reacts
to events already published to RabbitMQ by `core/events.py`'s
`event_publisher` (currently only `scheduling_engine.py` and
`emergency_engine.py` publish anything — see
`services/notification_engine.py`'s module docstring for the exact,
deliberate four-topic scope boundary this PRP draws).

`branch_id` is a schema addition this PRP made to the originally-scaffolded
`notifications` table (confirmed live against a fresh Postgres container:
`\d notifications` shows `branch_id uuid`, nullable, `REFERENCES
branches(id)`, plus `notifications_channel_valid`/`notifications_status_valid`
CHECK constraints and a `branch_isolation` RLS policy on the real column —
no join needed, unlike tables such as `beds`/`admissions` that resolve
tenancy transitively through a parent). It stays nullable in the ORM even
though every current topic handler always resolves one: a future topic that
genuinely can't resolve a branch should get a `NULL` row, not a crash (see
`services/notification_engine.py` design decision #4).

`NotificationChannel`/`NotificationStatus` are plain `String` columns, NOT
SQLAlchemy `Enum`/Postgres `CREATE TYPE ... AS ENUM` columns — schema.sql
declares both `channel` and `status` as plain `TEXT` with a `CHECK (... IN
(...))` constraint as the only DB-level guard, matching
`models/consultation.py`'s `Prescription.status` convention (a plain-TEXT
lifecycle column with a cheap CHECK backstop), NOT the
`models/ward.py`/`models/lab.py` convention (`BedStatus`/`LabOrderStatus`,
real Postgres `CREATE TYPE ... AS ENUM` columns). Confirmed by reading the
live DDL directly, not assumed from either prior pattern.

`payload` is `JSONB` (Postgres-specific type, same as
`PatientDuplicateCandidate.match_reason` in `models/patient.py`) — the raw
event payload dict is stored verbatim for audit/replay/manual-retry
purposes, not just the fields this module happened to need.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class NotificationChannel(str, enum.Enum):
    """Mirrors schema.sql's `notifications.channel` CHECK constraint
    (`CHECK (channel IN ('sms', 'email', 'push'))`). Plain `String` column
    on `Notification`, NOT a SQLAlchemy `Enum`/Postgres `CREATE TYPE` — see
    module docstring. `str` mixin (same pattern as
    `AppointmentStatus`/`InvoiceStatus`) so a channel serializes as its
    plain string value without a custom encoder."""

    sms = "sms"
    email = "email"
    push = "push"


class NotificationStatus(str, enum.Enum):
    """Mirrors schema.sql's `notifications.status` CHECK constraint
    (`CHECK (status IN ('queued', 'sent', 'failed'))`). Plain `String`
    column on `Notification`, NOT a SQLAlchemy `Enum`/Postgres `CREATE
    TYPE` — see module docstring.

    Lifecycle: `queued` (row created, before the provider call) ->
    `sent` (provider returned `True`) | `failed` (provider raised or
    returned `False`). There is no automatic retry/backoff (see the PRP's
    design decision #2) — a `failed` row is retried manually via `PATCH
    /notifications/{id}/retry`, a Phase 2 concern.
    """

    queued = "queued"
    sent = "sent"
    failed = "failed"


class Notification(Base, UUIDPrimaryKeyMixin):
    """One record of an attempted out-of-band notification (SMS/email/push)
    triggered by a domain event. Created by
    `services/notification_engine.py`'s `handle_event` with
    `status="queued"` and committed immediately (so the row exists even if
    the subsequent provider call fails), then updated in place to `"sent"`
    or `"failed"` once the provider has been called.

    `user_id`/`patient_id` are both nullable and independent — a given
    topic handler populates whichever one it can resolve a recipient from
    (`patient_id` -> `Patient.phone` for SMS, `user_id` -> `User.email` for
    email/push), never both. No CHECK constraint requires at least one to
    be set in schema.sql, so the ORM doesn't invent one either — an
    unresolvable recipient is a `notification_engine.py` concern (it would
    fail the provider call with no valid recipient string), not a DB-level
    guard here.
    """

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    channel: Mapped[str] = mapped_column(String, nullable=False)
    template: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default=NotificationStatus.queued.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
