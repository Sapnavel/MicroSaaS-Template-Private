"""ORM models for the Clinical Consultation & Smart Prescription Engine module
(database/schema.sql §7: `consultations`, `diagnoses`, `patient_allergies`,
`drugs`, `drug_interactions`, `prescriptions`, `prescription_items`).

Same situation the Patient module found `patient_duplicate_candidates` in
(see `PatientDuplicateCandidate`'s docstring in `models/patient.py`): all
seven tables have existed in schema.sql since the initial scaffold, but
nothing read/wrote any of them until this PRP (PRPs/clinical-consultation-
prescription-prp.md, Phase 1) -- there was no consultation/prescription
business logic yet to need them. This file adds the ORM layer only; Phase 2
(`services/consultation_service.py`, `routers/consultations.py`) owns the
actual read/write logic and endpoints.

Key design decision (per the PRP, mirrors the Patient module's own no-
`branch_id` reasoning): `Consultation` has no `branch_id` column in
schema.sql. Tenant scoping is implicit through `doctor_id` -- a doctor
belongs to exactly one branch (`Doctor.branch_id`) -- so `authorize()`'s
row-level tenant guard is a no-op here, same as it is for `Patient`. The
real access control is ownership-based (`doctor.id == consultation.doctor_id`,
see `core/security.py`'s `_doctor_reads_own_consultations`), not row-level
branch isolation. This is stated here, not just in the PRP, so it isn't lost
if this file is read on its own.

`Consultation.notes` is PHI (free-text clinical notes) and is encrypted at
rest via `EncryptedString`, mapped from the `notes_encrypted` BYTEA column --
same pattern as `Patient.phone` -> `phone_encrypted`. `Consultation.symptoms`
is deliberately left as plain `String`/`TEXT`: schema.sql does not encrypt
it (no `_encrypted` suffix, plain `TEXT` column), so no encryption is added
here that isn't in the DDL -- see the PRP's DATA MODEL section, which is
explicit that this is intentional, not an oversight.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class Consultation(Base, UUIDPrimaryKeyMixin):
    """One doctor-patient encounter, started from an `in_progress`
    appointment (enforced by Phase 2's `consultation_service.start_consultation`,
    not by a DB constraint beyond `appointment_id`'s `UNIQUE` -- one
    consultation per appointment).

    No `TimestampMixin` here: schema.sql gives this table its own
    `started_at`/`ended_at` pair instead of a generic `created_at`/
    `updated_at`, since a consultation's "creation time" and clinical start
    time are the same moment and there's no update-tracking need beyond
    `ended_at` marking completion.
    """

    __tablename__ = "consultations"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id"), unique=True, nullable=False
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)

    # Plain text, NOT PHI-encrypted -- matches schema.sql's `symptoms TEXT`
    # (no `_encrypted` suffix). See module docstring.
    symptoms: Mapped[str | None] = mapped_column(String)

    # PHI: encrypted at rest, transparently decrypted on read. Maps to the
    # `notes_encrypted BYTEA` column, same pattern as `Patient.phone`.
    notes: Mapped[str | None] = mapped_column("notes_encrypted", EncryptedString)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Diagnosis(Base, UUIDPrimaryKeyMixin):
    """A structured ICD-10/11 diagnosis recorded against a `Consultation`.

    `is_primary` uniqueness (at most one `True` per consultation) is NOT a DB
    constraint in schema.sql -- it's enforced at the service layer (Phase 2),
    per the PRP's endpoint table: a second `is_primary=true` POST is a 400,
    not silently auto-demoting the previous primary diagnosis.
    """

    __tablename__ = "diagnoses"

    consultation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consultations.id"), nullable=False
    )
    icd_code: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PatientAllergy(Base, UUIDPrimaryKeyMixin):
    """A patient's recorded allergy/adverse-reaction history. Nothing wrote
    to this table before this PRP (see module docstring); Phase 2 adds the
    write path (`POST /api/v1/patients/{patient_id}/allergies`), and
    `services/prescription_safety.py` (this same Phase) is the first reader.

    `severity` is plain `String`, not a SQLAlchemy/Postgres `Enum` -- schema.sql
    stores it as plain `TEXT` with a comment listing `mild, moderate, severe`
    rather than a `CREATE TYPE ... AS ENUM`, unlike e.g. `AppointmentStatus`.
    Matching the actual DDL here (not upgrading it silently) avoids an
    ORM/DDL mismatch of the kind the Auth module's TEST-AGENT caught with
    `Branch`/`HospitalGroup` missing `updated_at`.
    """

    __tablename__ = "patient_allergies"

    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    substance: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    reaction: Mapped[str | None] = mapped_column(String)


class Drug(Base, UUIDPrimaryKeyMixin):
    """A drug reference row. Seeded (see `database/seed_clinical_reference_data.sql`)
    with a small illustrative scaffold set -- see that file's header and
    `services/prescription_safety.py`'s module docstring for why this is not
    a real drug database.
    """

    __tablename__ = "drugs"

    name: Mapped[str] = mapped_column(String, nullable=False)
    generic_name: Mapped[str | None] = mapped_column(String)
    # Used by the interaction/allergy-class checker in
    # services/prescription_safety.py -- see that module's docstring for the
    # documented false-negative gap this implies.
    interaction_class: Mapped[str | None] = mapped_column(String)


class DrugInteraction(Base):
    """A known interaction between an unordered pair of drugs.

    Composite primary key (`drug_a_id`, `drug_b_id`) -- matches schema.sql's
    `PRIMARY KEY (drug_a_id, drug_b_id)` exactly; there is no synthetic `id`
    column on this table (unlike every other model here), so this
    deliberately does NOT use `UUIDPrimaryKeyMixin`.

    No ordering guarantee between `drug_a_id`/`drug_b_id`: unlike
    `patient_duplicate_candidates` (where `patient_service._ordered_pair`
    enforces a canonical sort order at *write* time, because the service
    layer controls every insert), this is reference/lookup data seeded by
    `database/seed_clinical_reference_data.sql` where insert order isn't
    controlled the same way. Callers must query both `(a, b)` and `(b, a)`
    orderings -- see `services/prescription_safety.py`'s
    `check_drug_interactions`, which does exactly that -- rather than
    relying on any single ordering.
    """

    __tablename__ = "drug_interactions"

    drug_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drugs.id"), primary_key=True
    )
    drug_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drugs.id"), primary_key=True
    )
    severity: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)


class Prescription(Base, UUIDPrimaryKeyMixin):
    """A prescription written during a `Consultation`. `status` progresses
    draft -> finalized -> dispensed (Pharmacy module, out of scope here);
    Phase 2's `create_prescription` creates rows directly as `finalized`
    per the PRP's endpoint flow (no separate draft-then-finalize step in
    this module's MVP).

    Plain `String`, not a SQLAlchemy `Enum`, matching schema.sql's
    `status TEXT NOT NULL DEFAULT 'draft'` (no `CREATE TYPE` for this
    column, same reasoning as `PatientAllergy.severity`).
    """

    __tablename__ = "prescriptions"

    consultation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consultations.id"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PrescriptionItem(Base, UUIDPrimaryKeyMixin):
    """One drug line item within a `Prescription`.

    `duration_days` is nullable -- `NULL` means an ongoing/chronic
    medication with no defined end date, which `services/prescription_safety.py`'s
    "active prescription" window treats as always-active (never expires).
    See that module's `check_drug_interactions` docstring for the exact
    date-arithmetic this implies.

    The `CHECK` constraint (mirrors `database/schema.sql`) is defense-in-depth
    alongside `PrescriptionItemCreate`'s `Field(ge=1)` -- a zero/negative
    value would make `course_end` compute as already-expired the instant
    it's written, silently hiding a real drug from the interaction checker
    (REVIEW-AGENT finding H1, PRPs/clinical-consultation-prescription-prp.md
    Phase 3).
    """

    __tablename__ = "prescription_items"
    __table_args__ = (CheckConstraint("duration_days IS NULL OR duration_days > 0"),)

    prescription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id"), nullable=False
    )
    drug_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("drugs.id"), nullable=False)
    dosage: Mapped[str] = mapped_column(String, nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    duration_days: Mapped[int | None] = mapped_column(SmallInteger)
