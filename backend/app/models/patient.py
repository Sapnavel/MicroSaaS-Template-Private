import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString
from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Patient(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "patients"

    mrn: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    dob: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[str] = mapped_column(String, nullable=False)

    # PHI: encrypted at rest, transparently decrypted on read.
    phone: Mapped[str] = mapped_column("phone_encrypted", EncryptedString, nullable=False)
    national_id: Mapped[str | None] = mapped_column("national_id_encrypted", EncryptedString)
    address: Mapped[str | None] = mapped_column("address_encrypted", EncryptedString)
    allergies_note: Mapped[str | None] = mapped_column("allergies_encrypted", EncryptedString)

    # Deterministic hashes for dedup matching without decrypting PHI.
    phone_hash: Mapped[str | None] = mapped_column(String)
    national_id_hash: Mapped[str | None] = mapped_column(String)

    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"))

    # Added by BACKEND-AGENT, PRPs/patient-master-index-prp.md Phase 1.
    # Links a portal login (Auth module, app.models.user.User, role=patient)
    # to this canonical clinical record. Nullable both ways: a walk-in
    # patient registered by front desk may have no portal login yet, and a
    # portal signup may exist before any clinical visit creates a Patient
    # Master Index record. UNIQUE because one login maps to at most one
    # canonical patient. Note `patients` intentionally has no `branch_id`
    # (see module docstring in services/patient_matching.py) -- this FK
    # does not change that, it only links identity, not tenancy.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True)


class PatientDuplicateCandidate(Base, UUIDPrimaryKeyMixin):
    """ORM model for `patient_duplicate_candidates` (database/schema.sql §2).

    Added by BACKEND-AGENT, PRPs/patient-master-index-prp.md Phase 2 -- this
    table existed in schema.sql since the initial scaffold (per the PRP's
    module overview) but had no ORM model behind it until now, since nothing
    read/wrote it before `services/patient_service.py`. Each row is one
    scored candidate pair surfaced by
    `patient_matching.scan_for_duplicates()`; `services/patient_service.py`
    owns all writes to this table (insert-on-create, merge/reject on review).

    `patient_a_id`/`patient_b_id` are stored in a deterministic (sorted-UUID)
    order by the service layer -- see `patient_service._ordered_pair` --
    both so the `UNIQUE (patient_a_id, patient_b_id)` constraint actually
    prevents duplicate-pair rows regardless of which patient was created
    first, and so `patient_a`/`patient_b` here have no clinical meaning
    (neither is "the original" or "the duplicate") -- that's decided at
    merge time via `survivor_id`, not by this ordering.
    """

    __tablename__ = "patient_duplicate_candidates"

    patient_a_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    patient_b_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    # asdecimal=False: this column stores a Python float computed by
    # `patient_matching.score_candidate` (weights/similarity are all
    # float math) -- without asdecimal=False, SQLAlchemy's default Numeric
    # behavior returns `decimal.Decimal` on read, which would silently
    # mismatch the `float` type this is `Mapped[float]` as, and complicate
    # both the pure-Python scoring tests and the `MergeResponse`/
    # `DuplicateCandidateResponse` Pydantic schemas downstream.
    match_score: Mapped[float] = mapped_column(Numeric(4, 3, asdecimal=False), nullable=False)
    match_reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    patient_a = relationship(Patient, foreign_keys=[patient_a_id])
    patient_b = relationship(Patient, foreign_keys=[patient_b_id])
