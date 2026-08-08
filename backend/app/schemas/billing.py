"""Pydantic request/response schemas for the Billing, Ledger & Insurance
Claims module (PRPs/billing-module-prp.md, Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT, working
in parallel against the same PRP -- do not rename fields without updating
the frontend in lockstep.

`ChargeableEventResponse` deliberately has NO suggested-amount field: this
system has no fee schedule, `billing_admin` enters the amount manually on
`POST /invoices/{id}/items` (see the PRP's design decision #6 and the
ENGINE DESIGN section's `list_chargeable_events` note) -- do not invent a
pricing field to "helpfully" pre-fill one.
"""

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# --- Requests ----------------------------------------------------------------


class InvoiceCreate(BaseModel):
    """POST /api/v1/billing/invoices body."""

    model_config = ConfigDict(extra="forbid")

    patient_id: uuid.UUID
    branch_id: uuid.UUID


class InvoiceItemCreate(BaseModel):
    """POST /api/v1/billing/invoices/{id}/items body. `amount` is validated
    `> 0` here at the schema layer (`Field(gt=0)`) as well as by the DB's
    `invoice_items` CHECK constraint and `billing_engine`'s own insert --
    defense in depth, not a substitute for either."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["consultation", "lab_order", "prescription", "admission"]
    source_id: uuid.UUID
    description: str
    amount: Decimal = Field(gt=0)


class InvoiceSplitCreate(BaseModel):
    """POST /api/v1/billing/invoices/{id}/split body. `billing_engine.split_invoice`
    is the actual authority on `claim_amount + patient_copay ==
    invoice.total_amount` (exact decimal equality, 422 on mismatch) -- the
    `Field(ge=0)` here only rules out negative amounts at the wire level."""

    model_config = ConfigDict(extra="forbid")

    payer_name: str
    claim_amount: Decimal = Field(ge=0)
    patient_copay: Decimal = Field(ge=0)


class InvoiceAdjustmentsUpdate(BaseModel):
    """PATCH /api/v1/billing/invoices/{id}/adjustments body. HMS Project
    Completion Prompt gap ("tax and discount handling") --
    `billing_engine.apply_invoice_adjustments` is the actual authority on
    `discount_amount <= invoice.total_amount` (422 on violation) and on
    `status == "open"` (409 otherwise); the `Field` bounds here only rule
    out obviously-invalid wire values."""

    model_config = ConfigDict(extra="forbid")

    tax_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)


class ClaimStateUpdate(BaseModel):
    """PATCH /api/v1/billing/claims/{id}/state body. The `Literal` here is
    the full `ClaimState` enum's value set -- `billing_engine.set_claim_state`
    is the actual authority on which `(current, requested)` pairs are legal
    (`_LEGAL_CLAIM_TRANSITIONS`), a 409 `IllegalClaimStateTransition`
    otherwise."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["submitted", "adjudicating", "approved", "denied", "paid"]


# --- Responses -----------------------------------------------------------


class InvoiceResponse(BaseModel):
    """Shared shape returned by every invoice-mutating endpoint and embedded
    in `InvoiceDetailResponse`.

    `tax_amount`/`grand_total` are `@computed_field`s, NOT stored columns --
    see `billing_engine.apply_invoice_adjustments`'s docstring for why
    `grand_total` is deliberately never cached. Both are computed off
    `total_amount` (the itemized subtotal `billing_engine` maintains,
    unaffected by tax/discount -- `split_invoice`'s `claim_amount +
    patient_copay == total_amount` check is intentionally untouched by this
    HMS Project Completion Prompt addition, since insurance splits the
    clinical subtotal, not a tax-inclusive total)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    branch_id: uuid.UUID
    status: str
    total_amount: Decimal
    tax_rate_percent: Decimal | None = None
    discount_amount: Decimal = Decimal("0")
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tax_amount(self) -> Decimal:
        discounted_subtotal = self.total_amount - self.discount_amount
        if self.tax_rate_percent is None:
            return Decimal("0.00")
        return (discounted_subtotal * self.tax_rate_percent / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def grand_total(self) -> Decimal:
        return self.total_amount - self.discount_amount + self.tax_amount


class InvoiceItemResponse(BaseModel):
    """One line item, embedded in `InvoiceDetailResponse`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    description: str
    amount: Decimal


class InsuranceClaimResponse(BaseModel):
    """The claim attached to a `split` invoice, embedded in
    `InvoiceDetailResponse` (`None` if the invoice hasn't been split yet)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    payer_name: str
    claim_amount: Decimal
    patient_copay: Decimal
    state: str
    updated_at: datetime


class InvoiceDetailResponse(BaseModel):
    """GET /api/v1/billing/invoices/{id} response: the invoice, all its
    items, and its claim if one exists (invoice not yet split -> `None`)."""

    invoice: InvoiceResponse
    items: list[InvoiceItemResponse]
    claim: InsuranceClaimResponse | None


class ClaimListItemResponse(BaseModel):
    """GET /api/v1/billing/claims response line (frontend UUID-to-dropdown
    conversion follow-up, backend phase) -- the first standalone list
    endpoint for `InsuranceClaim` rows (previously only reachable embedded
    inside `GET /billing/invoices/{id}`'s response). Built by hand in
    services/billing_service.py from an `InsuranceClaim` joined through
    `Invoice` to `Patient` (not plain `from_attributes` off an
    `InsuranceClaim` row alone -- `patient_name` lives on `Patient`, several
    joins away). `amount` mirrors `InsuranceClaimResponse.claim_amount`'s
    plain `Decimal` serialization exactly -- the established
    money-serialization convention in this schema module."""

    id: uuid.UUID
    invoice_id: uuid.UUID
    patient_name: str
    state: str
    amount: Decimal


class ChargeableEventResponse(BaseModel):
    """GET /api/v1/billing/patients/{patient_id}/chargeable-events response
    line -- a clinical event that is chargeable (per-type checks, see
    `services/billing_service.list_chargeable_events`'s docstring) and not
    yet present in any invoice's `invoice_items`. No suggested-amount field
    -- see module docstring."""

    source_type: str
    source_id: uuid.UUID
    suggested_description: str
    event_date: datetime
