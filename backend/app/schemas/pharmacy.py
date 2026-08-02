"""Pydantic request/response schemas for the Pharmacy Inventory module
(PRPs/pharmacy-module-prp.md, Phase 2).

Field names/casing here are a contract shared with FRONTEND-AGENT (see the
Phase 2 brief's "THE API CONTRACT" section) -- do not rename fields without
updating the frontend in lockstep.

`branch_id` is accepted (optional) on every write request body and every GET
filter -- `services/pharmacy_service._resolve_branch_id` /
`_resolve_branch_filter` decide what it actually means for a given caller
(pharmacist: must match their own branch or be omitted; system_admin: must
be supplied on writes, optional on reads where omission means "all
branches"). See that module's docstring for the full rule; this file only
defines the wire shape.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

# --- Requests ----------------------------------------------------------------


class InventoryItemCreate(BaseModel):
    """POST /api/v1/pharmacy/items body. Find-or-update semantics -- see
    `services/pharmacy_service.create_or_update_item` -- because
    `InventoryItem` has a `UNIQUE(branch_id, drug_id)` constraint."""

    model_config = ConfigDict(extra="forbid")

    drug_id: uuid.UUID
    reorder_threshold: int = Field(ge=0)
    branch_id: uuid.UUID | None = None


class BatchCreate(BaseModel):
    """POST /api/v1/pharmacy/batches body. `quantity` must be strictly
    positive at the schema level (`Field(gt=0)`) -- a batch being received
    should never be zero/negative, distinct from the FEFO engine's own
    quantity>0 check, which guards *dispense* requests, not receipts."""

    model_config = ConfigDict(extra="forbid")

    drug_id: uuid.UUID
    batch_number: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    expiry_date: date
    branch_id: uuid.UUID | None = None


class DispenseRequest(BaseModel):
    """POST /api/v1/pharmacy/dispense body. `reference` is free text (or an
    id string) -- purely informational, stored in the audit log's metadata,
    never FK-validated."""

    model_config = ConfigDict(extra="forbid")

    drug_id: uuid.UUID
    quantity: int = Field(gt=0)
    reference: str | None = None
    branch_id: uuid.UUID | None = None


# --- Responses -----------------------------------------------------------


class InventoryItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    reorder_threshold: int


class BatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    inventory_item_id: uuid.UUID
    batch_number: str
    quantity: int
    expiry_date: date
    received_at: datetime


class AllocationResponse(BaseModel):
    """One line of a dispense's per-batch FEFO breakdown -- mirrors
    `pharmacy_engine.BatchAllocationDetail` field-for-field, so
    `services/pharmacy_service.dispense` can build these directly from the
    engine's result without any extra shaping."""

    model_config = ConfigDict(from_attributes=True)

    batch_id: uuid.UUID
    batch_number: str
    expiry_date: date
    quantity_taken: int


class DispenseResponse(BaseModel):
    inventory_item_id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    quantity_dispensed: int
    allocations: list[AllocationResponse]


class LowStockItemResponse(BaseModel):
    """GET /api/v1/pharmacy/low-stock response line. Built by hand in
    `services/pharmacy_service.get_low_stock` from a joined query row (not
    `from_attributes` off an ORM instance -- there is no single ORM object
    with all of these fields, it's a join across `InventoryItem`/`Drug` plus
    an aggregated batch-quantity sum)."""

    inventory_item_id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    drug_name: str
    current_quantity: int
    reorder_threshold: int


class ExpiringBatchResponse(BaseModel):
    """GET /api/v1/pharmacy/expiring response line. Same "built from a
    joined query row" note as `LowStockItemResponse` above."""

    batch_id: uuid.UUID
    inventory_item_id: uuid.UUID
    drug_id: uuid.UUID
    drug_name: str
    batch_number: str
    quantity: int
    expiry_date: date
