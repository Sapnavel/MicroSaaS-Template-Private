import axios from "axios";

import { api } from "./api";
import type { DispenseAllocation, DispenseResult, ExpiringBatch, InventoryBatch, InventoryItem, LowStockItem } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Pharmacy Inventory module -- see
 * PRPs/pharmacy-module-prp.md "ENDPOINTS". Every exported function converts
 * to/from the camelCase types in `types/index.ts` at the boundary, same
 * pattern as `labService.ts` / `consultationService.ts`.
 *
 * `branchId` handling (see the PRP's branch-tenancy design decision): for a
 * `pharmacist` caller, `branchId` is always omitted from the request --
 * the backend derives it from the caller's own branch and 422s on a
 * mismatched explicit value, so the safest client behavior is to simply
 * never send the key. A `system_admin` caller (who has no branch of their
 * own) supplies it explicitly on every write endpoint and on the two GET
 * endpoints below. Every input type below models this as an optional
 * `branchId?: string`, and every function omits the wire key entirely
 * (rather than sending `null`) when it isn't provided, so a pharmacist's
 * request body/query never even contains a `branch_id` key.
 */

interface InventoryItemWire {
  id: string;
  branch_id: string;
  drug_id: string;
  reorder_threshold: number;
}

interface InventoryBatchWire {
  id: string;
  inventory_item_id: string;
  batch_number: string;
  quantity: number;
  expiry_date: string;
  received_at: string;
}

interface DispenseAllocationWire {
  batch_id: string;
  batch_number: string;
  expiry_date: string;
  quantity_taken: number;
}

interface DispenseResultWire {
  inventory_item_id: string;
  branch_id: string;
  drug_id: string;
  quantity_dispensed: number;
  allocations: DispenseAllocationWire[];
}

interface LowStockItemWire {
  inventory_item_id: string;
  branch_id: string;
  drug_id: string;
  drug_name: string;
  current_quantity: number;
  reorder_threshold: number;
}

interface ExpiringBatchWire {
  batch_id: string;
  inventory_item_id: string;
  drug_id: string;
  drug_name: string;
  batch_number: string;
  quantity: number;
  expiry_date: string;
}

/** Shape of the 409 body from `POST /pharmacy/dispense` -- insufficient stock. */
interface InsufficientStockWire {
  detail: string;
  available: number;
  requested: number;
}

/** Shape of the 404 body from `POST /pharmacy/dispense` -- unknown item. */
interface DetailOnlyWire {
  detail: string;
}

export interface CreateOrUpdateItemData {
  drugId: string;
  reorderThreshold: number;
  /** system_admin only -- see the file-level doc comment. */
  branchId?: string;
}

export interface ReceiveBatchData {
  drugId: string;
  batchNumber: string;
  quantity: number;
  expiryDate: string;
  /** system_admin only -- see the file-level doc comment. */
  branchId?: string;
}

export interface DispenseData {
  drugId: string;
  quantity: number;
  reference?: string;
  /** system_admin only -- see the file-level doc comment. */
  branchId?: string;
}

/**
 * Thrown on a 409 from `dispense()` -- the requested quantity exceeds the
 * available (non-expired) stock. Carries the backend's actual `available`/
 * `requested` numbers so the UI can show the pharmacist the real shortfall
 * ("only 12 available, 30 requested") rather than a generic "insufficient
 * stock" message -- mirrors the shape of `consultationService.ts`'s
 * `PrescriptionOverrideRequiredError` (a typed error class carrying the
 * structured 409 payload).
 */
export class InsufficientStockError extends Error {
  available: number;
  requested: number;

  constructor(message: string, available: number, requested: number) {
    super(message);
    this.name = "InsufficientStockError";
    this.available = available;
    this.requested = requested;
  }
}

/**
 * Thrown on a 404 from `dispense()` -- no `InventoryItem` exists at all for
 * this (branch, drug) pair, i.e. this branch has never stocked this drug.
 * Kept distinct from `InsufficientStockError` since the remediation is
 * different (receive stock for this drug first, vs. order more of what's
 * already stocked).
 */
export class UnknownInventoryItemError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UnknownInventoryItemError";
  }
}

function toInventoryItem(wire: InventoryItemWire): InventoryItem {
  return {
    id: wire.id,
    branchId: wire.branch_id,
    drugId: wire.drug_id,
    reorderThreshold: wire.reorder_threshold,
  };
}

function toInventoryBatch(wire: InventoryBatchWire): InventoryBatch {
  return {
    id: wire.id,
    inventoryItemId: wire.inventory_item_id,
    batchNumber: wire.batch_number,
    quantity: wire.quantity,
    expiryDate: wire.expiry_date,
    receivedAt: wire.received_at,
  };
}

function toDispenseAllocation(wire: DispenseAllocationWire): DispenseAllocation {
  return {
    batchId: wire.batch_id,
    batchNumber: wire.batch_number,
    expiryDate: wire.expiry_date,
    quantityTaken: wire.quantity_taken,
  };
}

function toDispenseResult(wire: DispenseResultWire): DispenseResult {
  return {
    inventoryItemId: wire.inventory_item_id,
    branchId: wire.branch_id,
    drugId: wire.drug_id,
    quantityDispensed: wire.quantity_dispensed,
    allocations: wire.allocations.map(toDispenseAllocation),
  };
}

function toLowStockItem(wire: LowStockItemWire): LowStockItem {
  return {
    inventoryItemId: wire.inventory_item_id,
    branchId: wire.branch_id,
    drugId: wire.drug_id,
    drugName: wire.drug_name,
    currentQuantity: wire.current_quantity,
    reorderThreshold: wire.reorder_threshold,
  };
}

function toExpiringBatch(wire: ExpiringBatchWire): ExpiringBatch {
  return {
    batchId: wire.batch_id,
    inventoryItemId: wire.inventory_item_id,
    drugId: wire.drug_id,
    drugName: wire.drug_name,
    batchNumber: wire.batch_number,
    quantity: wire.quantity,
    expiryDate: wire.expiry_date,
  };
}

export async function createOrUpdateItem(data: CreateOrUpdateItemData): Promise<InventoryItem> {
  const body = {
    drug_id: data.drugId,
    reorder_threshold: data.reorderThreshold,
    ...(data.branchId ? { branch_id: data.branchId } : {}),
  };
  const { data: wire } = await api.post<InventoryItemWire>("/api/v1/pharmacy/items", body);
  return toInventoryItem(wire);
}

export async function receiveBatch(data: ReceiveBatchData): Promise<InventoryBatch> {
  const body = {
    drug_id: data.drugId,
    batch_number: data.batchNumber,
    quantity: data.quantity,
    expiry_date: data.expiryDate,
    ...(data.branchId ? { branch_id: data.branchId } : {}),
  };
  const { data: wire } = await api.post<InventoryBatchWire>("/api/v1/pharmacy/batches", body);
  return toInventoryBatch(wire);
}

/**
 * Throws `InsufficientStockError` on a 409 (not enough non-expired stock)
 * and `UnknownInventoryItemError` on a 404 (this branch never stocked this
 * drug) -- callers should `instanceof`-branch on these before falling back
 * to `extractErrorMessage` for anything else, same pattern as
 * `PrescriptionPage.tsx`'s handling of the safety-check errors.
 */
export async function dispense(data: DispenseData): Promise<DispenseResult> {
  const body = {
    drug_id: data.drugId,
    quantity: data.quantity,
    reference: data.reference ?? null,
    ...(data.branchId ? { branch_id: data.branchId } : {}),
  };
  try {
    const { data: wire } = await api.post<DispenseResultWire>("/api/v1/pharmacy/dispense", body);
    return toDispenseResult(wire);
  } catch (error) {
    if (axios.isAxiosError<InsufficientStockWire>(error) && error.response?.status === 409) {
      const body409 = error.response.data;
      throw new InsufficientStockError(body409.detail, body409.available, body409.requested);
    }
    if (axios.isAxiosError<DetailOnlyWire>(error) && error.response?.status === 404) {
      throw new UnknownInventoryItemError(error.response.data.detail);
    }
    throw error;
  }
}

export async function getLowStock(branchId?: string): Promise<LowStockItem[]> {
  const { data } = await api.get<LowStockItemWire[]>("/api/v1/pharmacy/low-stock", {
    params: branchId ? { branch_id: branchId } : undefined,
  });
  return data.map(toLowStockItem);
}

export async function getExpiring(withinDays?: number, branchId?: string): Promise<ExpiringBatch[]> {
  const params: Record<string, string | number> = {};
  if (withinDays !== undefined) {
    params.within_days = withinDays;
  }
  if (branchId) {
    params.branch_id = branchId;
  }
  const { data } = await api.get<ExpiringBatchWire[]>("/api/v1/pharmacy/expiring", {
    params: Object.keys(params).length > 0 ? params : undefined,
  });
  return data.map(toExpiringBatch);
}
