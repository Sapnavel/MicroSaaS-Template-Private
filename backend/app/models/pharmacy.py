"""ORM models for the Pharmacy Inventory module (database/schema.sql §9:
`inventory_items`, `inventory_batches`), PRPs/pharmacy-module-prp.md Phase 1.

Key design decision (per the PRP -- read before touching either model):
this is the first module in the build sequence where branch tenancy is
*real*, not a no-op. `Patient` and `Consultation`/`LabOrder` are all
deliberately branch-agnostic (see their own modules' docstrings), but a
physical batch of pills sitting in Branch A's pharmacy is not interchangeable
with the same drug sitting in Branch B's. `InventoryItem.branch_id` is
therefore a real, non-nullable FK, and `core/security.py`'s generic ABAC
tenant guard (built in the Auth module, mostly unused since — every prior
resource had a reason to opt out) finally applies here without a manual
ownership workaround. Phase 2 registers the actual `authorize()` policies;
this file only establishes the column the tenant guard keys off of.

`InventoryItem.drug_id` points at `drugs.id` -- the SAME reference table
`Consultation`/`Prescription` already use (see `models/consultation.py`'s
`Drug`). Do not create a second drugs table; Pharmacy, Consultation, and Lab
all share this one reference table by design.

This file only adds the ORM layer. `services/pharmacy_engine.py` (this same
Phase) is the FEFO dispense engine that reads/locks/decrements
`InventoryBatch` rows; `services/pharmacy_service.py` (Phase 2) owns
find-or-create semantics for `InventoryItem`, commits, and audit logging.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class InventoryItem(Base, UUIDPrimaryKeyMixin):
    """One drug's stock record for one branch -- the `(branch_id, drug_id)`
    pair is unique (mirrors schema.sql's `UNIQUE (branch_id, drug_id)`
    exactly, via the `UniqueConstraint` below), so "receiving stock" for a
    drug this branch has stocked before must find-and-update this row, not
    insert a second one. That find-or-create logic is a service-layer
    concern (Phase 2's `pharmacy_service.receive_batch`) -- this model only
    establishes the constraint that makes a silent duplicate impossible even
    if the service layer ever gets that wrong.

    `reorder_threshold` has no meaning enforced at this layer; it is read by
    the low-stock query (Phase 2) to flag `SUM(batch.quantity) <
    reorder_threshold` for a branch's pharmacist.
    """

    __tablename__ = "inventory_items"
    __table_args__ = (UniqueConstraint("branch_id", "drug_id"),)

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    drug_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("drugs.id"), nullable=False)
    reorder_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class InventoryBatch(Base, UUIDPrimaryKeyMixin):
    """One expiry-dated lot of stock against an `InventoryItem`. This is the
    row the FEFO dispense engine (`services/pharmacy_engine.py`) locks and
    decrements.

    The `CheckConstraint("quantity >= 0")` is defense-in-depth alongside the
    engine's own "sum locked candidates before decrementing, never decrement
    below zero" logic -- mirrors schema.sql's `quantity INTEGER NOT NULL
    CHECK (quantity >= 0)` exactly, and mirrors the same discipline the
    Clinical Consultation module's REVIEW-AGENT established for
    `PrescriptionItem.duration_days` (see `models/consultation.py`): a DB
    CHECK constraint this codebase has been bitten by omitting from the ORM
    before gets an explicit `__table_args__` entry here so a future bug in
    the service layer fails loudly (`IntegrityError`) instead of silently
    persisting a negative quantity.

    No `updated_at`/`TimestampMixin` -- schema.sql gives this table only
    `received_at` (set once, at insert time); a batch's quantity changes via
    dispense but the DDL tracks no "last modified" timestamp for that, so
    none is added here that isn't in the DDL (same "don't upgrade the schema
    silently" discipline `PatientAllergy.severity` follows).
    """

    __tablename__ = "inventory_batches"
    __table_args__ = (CheckConstraint("quantity >= 0"),)

    inventory_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False
    )
    batch_number: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
