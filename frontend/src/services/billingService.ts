import { api } from "./api";
import type {
  ChargeableEvent,
  ClaimListItem,
  ClaimState,
  Invoice,
  InvoiceItem,
  InsuranceClaim,
  InvoiceStatus,
  SourceType,
} from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Billing, Ledger & Insurance
 * Claims module -- see PRPs/billing-module-prp.md "ENDPOINTS". Every
 * exported function converts to/from the camelCase types in
 * `types/index.ts` at the boundary, same pattern as `wardService.ts`.
 *
 * None of Billing's error bodies carry structured fields beyond `detail`
 * (unlike Ward's `IllegalBedStatusTransitionError`/`OTConflictError`), so
 * callers just use `extractErrorMessage(error, fallback)` from
 * `utils/errors.ts` -- no custom Error subclasses needed here.
 *
 * `branchId` handling mirrors `wardService.ts`/`pharmacyService.ts`: a
 * caller with their own branch never sends `branch_id` (the backend
 * derives it from the session), and only a `system_admin` supplies it
 * explicitly as a query param on the two branch-scoped GETs.
 */

/** `total_amount`/`tax_rate_percent`/`discount_amount`/`tax_amount`/
 * `grand_total` are `Decimal` fields on the backend -- Pydantic serializes
 * `Decimal` as a JSON **string** ("0.00"), not a number, to avoid float
 * precision loss (confirmed against the live API, not assumed -- every
 * other module's money-shaped fields use plain `float` and serialize as
 * numbers; billing is the only module using `Decimal`). The `toX` mappers
 * below are the one place that converts these to JS `number`s via
 * `Number(...)`, matching this file's own "wire format is only known here"
 * discipline. */
interface InvoiceWire {
  id: string;
  patient_id: string;
  branch_id: string;
  status: InvoiceStatus;
  total_amount: string;
  tax_rate_percent: string | null;
  discount_amount: string;
  tax_amount: string;
  grand_total: string;
  created_at: string;
}

interface InvoiceItemWire {
  id: string;
  invoice_id: string;
  source_type: SourceType;
  source_id: string;
  description: string;
  amount: string;
}

interface InsuranceClaimWire {
  id: string;
  invoice_id: string;
  payer_name: string;
  claim_amount: string;
  patient_copay: string;
  state: ClaimState;
  updated_at: string;
}

interface ChargeableEventWire {
  source_type: SourceType;
  source_id: string;
  suggested_description: string;
  event_date: string;
}

/** `GET /api/v1/billing/claims` list-item shape -- backs
 * `components/selects/ClaimSelect.tsx`. Distinct from `InsuranceClaimWire`
 * above (adds `patient_name`, omits the copay/claim-amount breakdown). */
interface ClaimListItemWire {
  id: string;
  invoice_id: string;
  patient_name: string;
  state: ClaimState;
  amount: string;
}

/** GET /billing/invoices/{id} response shape -- invoice + items + claim (if any). */
interface InvoiceDetailWire {
  invoice: InvoiceWire;
  items: InvoiceItemWire[];
  claim: InsuranceClaimWire | null;
}

export interface InvoiceDetail {
  invoice: Invoice;
  items: InvoiceItem[];
  claim: InsuranceClaim | null;
}

export interface CreateInvoiceData {
  patientId: string;
  branchId: string;
}

export interface AddInvoiceItemData {
  sourceType: SourceType;
  sourceId: string;
  description: string;
  amount: number;
}

export interface SplitInvoiceData {
  payerName: string;
  claimAmount: number;
  patientCopay: number;
}

export interface ListPatientInvoicesParams {
  branchId?: string;
}

export interface ListChargeableEventsParams {
  branchId?: string;
}

export interface ListClaimsParams {
  branchId: string;
  state?: ClaimState;
}

function toInvoice(wire: InvoiceWire): Invoice {
  return {
    id: wire.id,
    patientId: wire.patient_id,
    branchId: wire.branch_id,
    status: wire.status,
    totalAmount: Number(wire.total_amount),
    taxRatePercent: wire.tax_rate_percent !== null ? Number(wire.tax_rate_percent) : null,
    discountAmount: Number(wire.discount_amount),
    taxAmount: Number(wire.tax_amount),
    grandTotal: Number(wire.grand_total),
    createdAt: wire.created_at,
  };
}

function toInvoiceItem(wire: InvoiceItemWire): InvoiceItem {
  return {
    id: wire.id,
    invoiceId: wire.invoice_id,
    sourceType: wire.source_type,
    sourceId: wire.source_id,
    description: wire.description,
    amount: Number(wire.amount),
  };
}

function toInsuranceClaim(wire: InsuranceClaimWire): InsuranceClaim {
  return {
    id: wire.id,
    invoiceId: wire.invoice_id,
    payerName: wire.payer_name,
    claimAmount: Number(wire.claim_amount),
    patientCopay: Number(wire.patient_copay),
    state: wire.state,
    updatedAt: wire.updated_at,
  };
}

function toChargeableEvent(wire: ChargeableEventWire): ChargeableEvent {
  return {
    sourceType: wire.source_type,
    sourceId: wire.source_id,
    suggestedDescription: wire.suggested_description,
    eventDate: wire.event_date,
  };
}

function toClaimListItem(wire: ClaimListItemWire): ClaimListItem {
  return {
    id: wire.id,
    invoiceId: wire.invoice_id,
    patientName: wire.patient_name,
    state: wire.state,
    amount: Number(wire.amount),
  };
}

export async function createInvoice(data: CreateInvoiceData): Promise<Invoice> {
  const body = { patient_id: data.patientId, branch_id: data.branchId };
  const { data: wire } = await api.post<InvoiceWire>("/api/v1/billing/invoices", body);
  return toInvoice(wire);
}

export async function getInvoice(invoiceId: string): Promise<InvoiceDetail> {
  const { data: wire } = await api.get<InvoiceDetailWire>(`/api/v1/billing/invoices/${invoiceId}`);
  return {
    invoice: toInvoice(wire.invoice),
    items: wire.items.map(toInvoiceItem),
    claim: wire.claim ? toInsuranceClaim(wire.claim) : null,
  };
}

export async function listPatientInvoices(
  patientId: string,
  params: ListPatientInvoicesParams = {},
): Promise<Invoice[]> {
  const query: Record<string, string> = {};
  if (params.branchId) {
    query.branch_id = params.branchId;
  }
  const { data } = await api.get<InvoiceWire[]>(`/api/v1/billing/patients/${patientId}/invoices`, {
    params: Object.keys(query).length > 0 ? query : undefined,
  });
  return data.map(toInvoice);
}

export async function listChargeableEvents(
  patientId: string,
  params: ListChargeableEventsParams = {},
): Promise<ChargeableEvent[]> {
  const query: Record<string, string> = {};
  if (params.branchId) {
    query.branch_id = params.branchId;
  }
  const { data } = await api.get<ChargeableEventWire[]>(
    `/api/v1/billing/patients/${patientId}/chargeable-events`,
    { params: Object.keys(query).length > 0 ? query : undefined },
  );
  return data.map(toChargeableEvent);
}

export async function addInvoiceItem(invoiceId: string, data: AddInvoiceItemData): Promise<Invoice> {
  const body = {
    source_type: data.sourceType,
    source_id: data.sourceId,
    description: data.description,
    amount: data.amount,
  };
  const { data: wire } = await api.post<InvoiceWire>(`/api/v1/billing/invoices/${invoiceId}/items`, body);
  return toInvoice(wire);
}

export async function splitInvoice(invoiceId: string, data: SplitInvoiceData): Promise<Invoice> {
  const body = {
    payer_name: data.payerName,
    claim_amount: data.claimAmount,
    patient_copay: data.patientCopay,
  };
  const { data: wire } = await api.post<InvoiceWire>(`/api/v1/billing/invoices/${invoiceId}/split`, body);
  return toInvoice(wire);
}

/** `GET /api/v1/billing/claims?branch_id=&state=(optional)` -- backs
 * `components/selects/ClaimSelect.tsx`. */
export async function listClaims(params: ListClaimsParams): Promise<ClaimListItem[]> {
  const query: Record<string, string> = { branch_id: params.branchId };
  if (params.state) {
    query.state = params.state;
  }
  const { data } = await api.get<ClaimListItemWire[]>("/api/v1/billing/claims", { params: query });
  return data.map(toClaimListItem);
}

export async function setClaimState(claimId: string, state: ClaimState): Promise<InsuranceClaim> {
  const { data: wire } = await api.patch<InsuranceClaimWire>(`/api/v1/billing/claims/${claimId}/state`, {
    state,
  });
  return toInsuranceClaim(wire);
}

/** `POST /api/v1/billing/invoices/{id}/send-reminder` -- HMS Project
 * Completion Prompt gap fix: "Payment reminders" had no trigger anywhere.
 * Staff-triggered (there is no due-date/scheduler concept in this schema to
 * fire it automatically -- see billing_service.send_payment_reminder's
 * docstring). Available to front_desk too, not just billing_admin -- it
 * doesn't mutate the invoice, only publishes a notification event. */
export async function sendPaymentReminder(invoiceId: string): Promise<Invoice> {
  const { data: wire } = await api.post<InvoiceWire>(`/api/v1/billing/invoices/${invoiceId}/send-reminder`, {});
  return toInvoice(wire);
}

export async function markInvoicePaid(invoiceId: string): Promise<Invoice> {
  const { data: wire } = await api.patch<InvoiceWire>(`/api/v1/billing/invoices/${invoiceId}/mark-paid`, {});
  return toInvoice(wire);
}

export async function voidInvoice(invoiceId: string): Promise<Invoice> {
  const { data: wire } = await api.patch<InvoiceWire>(`/api/v1/billing/invoices/${invoiceId}/void`, {});
  return toInvoice(wire);
}

export interface ApplyInvoiceAdjustmentsData {
  taxRatePercent: number | null;
  discountAmount: number;
}

/** `PATCH /api/v1/billing/invoices/{id}/adjustments` -- HMS Project
 * Completion Prompt gap fix: "tax and discount handling". Open-invoices
 * only (backend 409s otherwise) -- `taxAmount`/`grandTotal` on the returned
 * `Invoice` are server-computed, never sent. */
export async function applyInvoiceAdjustments(
  invoiceId: string,
  data: ApplyInvoiceAdjustmentsData,
): Promise<Invoice> {
  const body = { tax_rate_percent: data.taxRatePercent, discount_amount: data.discountAmount };
  const { data: wire } = await api.patch<InvoiceWire>(`/api/v1/billing/invoices/${invoiceId}/adjustments`, body);
  return toInvoice(wire);
}

/** `GET /api/v1/billing/invoices/{id}/receipt` -- HMS Project Completion
 * Prompt gap fix: "receipt generation". Returns the raw PDF bytes as a
 * `Blob` (not JSON) -- `responseType: "blob"` is required so axios doesn't
 * try to parse binary content as text/JSON. Callers turn this into a
 * download via `URL.createObjectURL`. */
export async function fetchInvoiceReceiptPdf(invoiceId: string): Promise<Blob> {
  const { data } = await api.get<Blob>(`/api/v1/billing/invoices/${invoiceId}/receipt`, {
    responseType: "blob",
  });
  return data;
}
