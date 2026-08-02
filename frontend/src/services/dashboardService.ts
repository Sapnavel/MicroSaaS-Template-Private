import { api } from "./api";
import type {
  DailyRevenue,
  DepartmentWaitTime,
  ExpiringBatch,
  LowStockItem,
  NoShowRate,
  StockAlerts,
  WardOccupancy,
} from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Executive & Operational
 * Dashboard module -- see PRPs/executive-dashboard-prp.md "ENDPOINTS".
 * Every exported function converts to/from the camelCase types in
 * `types/index.ts` at the boundary, same pattern as every other
 * `*Service.ts` file (`notificationService.ts` / `billingService.ts`).
 *
 * This entire module is `system_admin`-only (design decision #1) -- every
 * one of the five params objects below carries an optional `branchId`:
 * omitted means "aggregate across every branch," supplied means "scope to
 * this one branch." There is no non-admin caller here, so (unlike
 * `pharmacyService.ts`/`billingService.ts`) there's no "derive from the
 * caller's own branch" case to model at all -- `branchId` is always
 * explicit-or-omitted, never implicit.
 */

interface WardOccupancyWire {
  branch_id: string;
  ward_id: string;
  ward_name: string;
  ward_type: string;
  total_beds: number;
  occupied_beds: number;
  occupancy_pct: number;
}

interface DepartmentWaitTimeWire {
  branch_id: string;
  department_id: number | null;
  department_name: string | null;
  avg_wait_minutes: number;
  sample_size: number;
}

interface DailyRevenueWire {
  branch_id: string;
  day: string;
  invoice_count: number;
  gross_billed: number;
  collected: number;
  outstanding: number;
}

interface NoShowRateWire {
  branch_id: string;
  total_considered: number;
  no_show_count: number;
  no_show_rate: number;
  no_show_risk_score_note: string;
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

interface StockAlertsWire {
  low_stock: LowStockItemWire[];
  expiring_soon: ExpiringBatchWire[];
}

export interface DashboardParams {
  branchId?: string;
  days?: number;
}

function toWardOccupancy(wire: WardOccupancyWire): WardOccupancy {
  return {
    branchId: wire.branch_id,
    wardId: wire.ward_id,
    wardName: wire.ward_name,
    wardType: wire.ward_type,
    totalBeds: wire.total_beds,
    occupiedBeds: wire.occupied_beds,
    occupancyPct: wire.occupancy_pct,
  };
}

function toDepartmentWaitTime(wire: DepartmentWaitTimeWire): DepartmentWaitTime {
  return {
    branchId: wire.branch_id,
    departmentId: wire.department_id,
    departmentName: wire.department_name,
    avgWaitMinutes: wire.avg_wait_minutes,
    sampleSize: wire.sample_size,
  };
}

function toDailyRevenue(wire: DailyRevenueWire): DailyRevenue {
  return {
    branchId: wire.branch_id,
    day: wire.day,
    invoiceCount: wire.invoice_count,
    grossBilled: wire.gross_billed,
    collected: wire.collected,
    outstanding: wire.outstanding,
  };
}

function toNoShowRate(wire: NoShowRateWire): NoShowRate {
  return {
    branchId: wire.branch_id,
    totalConsidered: wire.total_considered,
    noShowCount: wire.no_show_count,
    noShowRate: wire.no_show_rate,
    noShowRiskScoreNote: wire.no_show_risk_score_note,
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

function toStockAlerts(wire: StockAlertsWire): StockAlerts {
  return {
    lowStock: wire.low_stock.map(toLowStockItem),
    expiringSoon: wire.expiring_soon.map(toExpiringBatch),
  };
}

function buildQuery(params: DashboardParams): Record<string, string | number> | undefined {
  const query: Record<string, string | number> = {};
  if (params.branchId) {
    query.branch_id = params.branchId;
  }
  if (params.days !== undefined) {
    query.days = params.days;
  }
  return Object.keys(query).length > 0 ? query : undefined;
}

export async function getOccupancy(params: DashboardParams = {}): Promise<WardOccupancy[]> {
  const { data } = await api.get<WardOccupancyWire[]>("/api/v1/dashboard/occupancy", {
    params: params.branchId ? { branch_id: params.branchId } : undefined,
  });
  return data.map(toWardOccupancy);
}

export async function getWaitTimes(params: DashboardParams = {}): Promise<DepartmentWaitTime[]> {
  const { data } = await api.get<DepartmentWaitTimeWire[]>("/api/v1/dashboard/wait-times", {
    params: buildQuery(params),
  });
  return data.map(toDepartmentWaitTime);
}

export async function getRevenue(params: DashboardParams = {}): Promise<DailyRevenue[]> {
  const { data } = await api.get<DailyRevenueWire[]>("/api/v1/dashboard/revenue", {
    params: buildQuery(params),
  });
  return data.map(toDailyRevenue);
}

export async function getNoShowRate(params: DashboardParams = {}): Promise<NoShowRate[]> {
  const { data } = await api.get<NoShowRateWire[]>("/api/v1/dashboard/no-shows", {
    params: buildQuery(params),
  });
  return data.map(toNoShowRate);
}

export async function getStockAlerts(params: DashboardParams = {}): Promise<StockAlerts> {
  const { data } = await api.get<StockAlertsWire>("/api/v1/dashboard/stock-alerts", {
    params: params.branchId ? { branch_id: params.branchId } : undefined,
  });
  return toStockAlerts(data);
}
