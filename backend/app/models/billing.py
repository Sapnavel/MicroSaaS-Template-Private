"""ORM models for the Billing / Ledger / Insurance Claims module
(database/schema.sql section 11: `invoices`, `invoice_items`,
`insurance_claims`), PRPs/billing-module-prp.md Phase 1.

This file only adds the ORM layer; the actual invoice/claim lifecycle
(create -> add items -> split -> mark-paid, void, and the claim state
machine) lives in `services/billing_engine.py` (this same Phase). Phase 2
adds `schemas/billing.py`, `services/billing_service.py`, and
`routers/billing.py`.

Key design decision (per the PRP's DATA MODEL section -- read this before
assuming otherwise): `InvoiceStatus` and `ClaimState` are plain `String`
columns, NOT SQLAlchemy `Enum`/Postgres `CREATE TYPE ... AS ENUM` columns.
This matches schema.sql exactly -- `invoices.status` and
`insurance_claims.state` are both declared `TEXT NOT NULL DEFAULT '...'`
with a `CHECK (... IN (...))` constraint as the only DB-level guard, the
same convention `models/consultation.py`'s `Prescription.status` uses (a
plain-TEXT lifecycle column with app-level validation plus a cheap CHECK
backstop) -- NOT the convention `models/ward.py`'s `BedStatus`/`bed_status`
or `models/lab.py`'s `LabOrderStatus`/`lab_sample_status` use (a real
Postgres `CREATE TYPE ... AS ENUM`). Confirmed by reading schema.sql's
actual DDL (section 11) rather than assumed from either prior pattern.

`InvoiceItem.__table_args__` mirrors schema.sql's
`CONSTRAINT invoice_items_source_unique UNIQUE (source_type, source_id)`
exactly -- the authoritative, DB-level guard against double-billing the
same clinical event across ANY invoice (not just within one invoice). See
`services/billing_engine.py`'s `add_invoice_item` for how the resulting
`IntegrityError` is caught and turned into `DuplicateChargeError`. This is
the money equivalent of the `EXCLUDE USING gist` two-layer discipline
(`Appointment`, `Admission`, `OTSchedule`) used everywhere else in this
codebase -- a plain `UNIQUE` suffices here since there's no time-range
overlap involved, just "this exact source event has already been
invoiced, full stop."

`source_id` on `InvoiceItem` deliberately has NO foreign key -- it is a
polymorphic reference whose target table depends on `source_type`
(`'consultation'` -> `consultations.id`, `'lab_order'` ->
`lab_orders.id`, `'prescription'` -> `prescriptions.id`, `'admission'` ->
`admissions.id`), matching schema.sql exactly (a single UUID column, no
FK). `services/billing_engine.py`'s `add_invoice_item` is what actually
validates the referenced row exists, belongs to the same patient as the
invoice, and is chargeable -- see design decisions #4 and #6 in the PRP.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class InvoiceStatus(str, enum.Enum):
    """Mirrors schema.sql's `invoices.status` CHECK constraint
    (`CHECK (status IN ('open', 'split', 'paid', 'void'))`). Plain `String`
    column on `Invoice`, NOT a SQLAlchemy `Enum`/Postgres `CREATE TYPE` --
    see module docstring. `str` mixin (same pattern as
    `AppointmentStatus`/`LabOrderStatus`/`BedStatus`) so a status serializes
    as its plain string value without a custom encoder.

    Lifecycle: `open` (created, items can be added) -> `split` (insurer +
    copay recorded via `split_invoice`) -> `paid` (via the separate, explicit
    `mark_invoice_paid` -- see PRP design decision #5 for why this is never
    automatic from claim state). `void` is reachable from `open` or `split`
    only (see `services/billing_engine.py`'s `void_invoice`) -- a `paid`
    invoice needs a real refund process, out of scope here.
    """

    open = "open"
    split = "split"
    paid = "paid"
    void = "void"


class ClaimState(str, enum.Enum):
    """Mirrors schema.sql's `insurance_claims.state` CHECK constraint
    (`CHECK (state IN ('submitted', 'adjudicating', 'approved', 'denied',
    'paid'))`). Plain `String` column on `InsuranceClaim`, NOT a SQLAlchemy
    `Enum`/Postgres `CREATE TYPE` -- see module docstring.

    Legal transitions are enforced by `services/billing_engine.py`'s
    `set_claim_state` against `_LEGAL_CLAIM_TRANSITIONS` (submitted ->
    adjudicating|denied, adjudicating -> approved|denied, approved -> paid).
    `denied` and `paid` are both terminal -- never a legal source state for
    any further transition.
    """

    submitted = "submitted"
    adjudicating = "adjudicating"
    approved = "approved"
    denied = "denied"
    paid = "paid"


class Invoice(Base, UUIDPrimaryKeyMixin):
    """One patient's bill at one branch, aggregating chargeable clinical
    events as `InvoiceItem` rows. `total_amount` is a derived cache,
    recomputed fresh from `SUM(invoice_items.amount)` after every insert by
    `services/billing_engine.py`'s `add_invoice_item` -- NEVER incremented
    in place (see the PRP's design decision #2, and that engine module's
    docstring, for the "derived field silently drifts from its source of
    truth" bug class this avoids).

    `branch_id` is a real column, set at creation time to the branch where
    the invoice was opened -- `patients` intentionally has no `branch_id`
    of its own (see `models/patient.py`), so this is the actual tenant
    scope for this table and its dependents (`InvoiceItem`,
    `InsuranceClaim`, both scoped transitively through `invoice_id`, same
    "no branch_id column of its own" pattern `beds`/`admissions` use under
    `wards`).
    """

    __tablename__ = "invoices"

    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=InvoiceStatus.open.value)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    # HMS Project Completion Prompt gap ("tax and discount handling") --
    # see schema.sql's `invoices` DDL comment for the exact semantics
    # (invoice-level, discount applied before tax, both default to a no-op
    # so every pre-existing invoice is unaffected).
    tax_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True, default=None)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InvoiceItem(Base, UUIDPrimaryKeyMixin):
    """One chargeable line item on an `Invoice`, polymorphically referencing
    a `Consultation`/`LabOrder`/`Prescription`/`Admission` row via
    `(source_type, source_id)` -- see module docstring for why `source_id`
    carries no FK.

    `UniqueConstraint("source_type", "source_id")` mirrors schema.sql's
    `invoice_items_source_unique` constraint exactly -- SQLAlchemy's own
    metadata must match the live DDL so Alembic never proposes a spurious
    diff, the same discipline `Admission.__table_args__`'s
    `ExcludeConstraint` mirrors schema.sql for the Ward module.
    """

    __tablename__ = "invoice_items"
    __table_args__ = (UniqueConstraint("source_type", "source_id", name="invoice_items_source_unique"),)

    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class InsuranceClaim(Base, UUIDPrimaryKeyMixin):
    """One insurer claim against a `split` `Invoice`'s insurer portion.
    `claim_amount + patient_copay` is validated (exact decimal equality)
    against `invoice.total_amount` at creation time by
    `services/billing_engine.py`'s `split_invoice` -- not re-validated here,
    since this table has no CHECK spanning both amounts (schema.sql only
    guards each amount's own non-negativity).

    `state` transitions are tracked independently of `Invoice.status` -- see
    PRP design decision #5: there is no `payments` ledger table in this
    schema, so a claim reaching `paid` does not by itself mean the invoice's
    entire balance (insurer + copay) has been collected. `mark_invoice_paid`
    is the sole, explicit path to `Invoice.status == "paid"`.
    """

    __tablename__ = "insurance_claims"

    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    payer_name: Mapped[str] = mapped_column(String, nullable=False)
    claim_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    patient_copay: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    state: Mapped[str] = mapped_column(String, nullable=False, default=ClaimState.submitted.value)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
