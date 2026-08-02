"""ORM models for the Lab module (database/schema.sql §8: `lab_orders`,
`lab_samples`), PRPs/lab-module-prp.md Phase 1.

Key design decision (per the PRP -- read this before touching either model):
the `lab_sample_status` enum column lives on **`lab_orders.status`**, not on
`lab_samples`. `lab_samples` carries no status of its own -- instead it
accumulates *timestamp+actor evidence* for the steps that have an actor
(`collected_by`/`collected_at`, `verified_by`/`verified_at`), plus two
timestamps with no actor column (`processed_at`, `attached_to_emr_at`). Read
this as: the lifecycle status is a property of the *order*; the *sample* row
is where per-step evidence -- who, when, and eventually the result -- piles
up. This file only adds the ORM layer; the actual state-machine validation
lives in `services/lab_workflow.py` (Phase 1) and the DB writes/audit-logging
in `services/lab_service.py` (Phase 2, not built here).

`lab_samples.lab_order_id` is UNIQUE (added by REVIEW-AGENT, Phase 3, finding
H1) -- earlier Phase 1/2 work assumed no such constraint existed and relied
solely on an app-level "does a sample already exist?" check, which had an
unguarded TOCTOU race: two near-simultaneous `ordered -> collected`
transitions could both pass that check and both insert, leaving two sample
rows for one order and breaking every later read with `MultipleResultsFound`.
The DB constraint is now the authoritative backstop; `services/lab_service.py`
additionally takes a row lock on the `LabOrder` for defense in depth (same
two-layer discipline `services/scheduling_engine.py` uses: a lock for
fail-fast behavior, a DB constraint for actual correctness).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class LabOrderStatus(str, enum.Enum):
    """Mirrors Postgres `lab_sample_status` (see schema.sql §8:
    `CREATE TYPE lab_sample_status AS ENUM ('ordered', 'collected',
    'processing', 'verified', 'attached')`). `str` mixin (same pattern as
    `AppointmentStatus`/`TokenStatus`) so a status serializes as its plain
    string value without a custom encoder.

    This genuinely IS a Postgres `CREATE TYPE ... AS ENUM`, unlike
    `PatientAllergy.severity` / `Prescription.status` in the Consultation
    module, which are plain `TEXT` -- confirmed by reading schema.sql's
    actual DDL rather than assuming from a similar-looking column elsewhere.
    """

    ordered = "ordered"
    collected = "collected"
    processing = "processing"
    verified = "verified"
    attached = "attached"


class LabOrder(Base, UUIDPrimaryKeyMixin):
    """One lab test ordered against a `Consultation`. `status` is the
    forward-only lifecycle position (see `services/lab_workflow.py` for the
    transition table); this is the ONLY status column for the whole
    order-to-result lifecycle -- `LabSample` has none.

    GOTCHA (flagged explicitly because it has bitten this codebase before in
    other forms): the underlying Postgres enum type is named
    `lab_sample_status`, NOT `lab_order_status`, even though it is applied to
    `lab_orders.status` rather than any column on `lab_samples`. This is the
    schema's actual `CREATE TYPE` name (see schema.sql §8) -- the ORM's
    `Enum(..., name=...)` must match it exactly, or Alembic/SQLAlchemy will
    either fail to find the existing type or try to create a duplicate one.
    """

    __tablename__ = "lab_orders"

    consultation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consultations.id"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    test_code: Mapped[str] = mapped_column(String, nullable=False)
    ordered_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status: Mapped[LabOrderStatus] = mapped_column(
        Enum(LabOrderStatus, name="lab_sample_status"),
        nullable=False,
        default=LabOrderStatus.ordered,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LabSample(Base, UUIDPrimaryKeyMixin):
    """Per-step evidence (actor + timestamp, and eventually the result) for a
    `LabOrder`'s physical sample. No status column here -- see module
    docstring and `LabOrder.status`.

    Column-by-column notes, matched against schema.sql §8:
      - `collected_by`/`collected_at`: nullable, set by the `ordered ->
        collected` transition.
      - `processed_at`: nullable, set by `collected -> processing`. There is
        deliberately no `processed_by` column in schema.sql -- the actor for
        this step is still audit-logged (Phase 2's `lab_service.py`, via
        `record_audit_event`), just not persisted as a foreign key on this
        row, because the column doesn't exist.
      - `verified_by`/`verified_at`: nullable, set by `processing ->
        verified` -- the same transition that submits `result`.
      - `result` (PHI): mapped from `result_encrypted` (`BYTEA` in
        schema.sql) via `EncryptedString`, same pattern as
        `Consultation.notes` -> `notes_encrypted`. Nullable until the verify
        transition.
      - `attached_to_emr_at`: nullable, set by `verified -> attached`.
    """

    __tablename__ = "lab_samples"

    # unique=True: REVIEW-AGENT finding H1 (PRPs/lab-module-prp.md Phase 3) --
    # mirrors database/schema.sql's UNIQUE constraint, the authoritative
    # backstop against two concurrent `ordered -> collected` transitions
    # both inserting a sample row for the same order.
    lab_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_orders.id"), nullable=False, unique=True
    )
    collected_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PHI: encrypted at rest, transparently decrypted on read. Maps to the
    # `result_encrypted BYTEA` column -- see module docstring.
    result: Mapped[str | None] = mapped_column("result_encrypted", EncryptedString)
    attached_to_emr_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
