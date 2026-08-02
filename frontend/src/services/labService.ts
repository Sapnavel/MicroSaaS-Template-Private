import { api } from "./api";
import type { LabOrder, LabOrderStatus, LabSample } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Lab module -- see
 * PRPs/lab-module-prp.md "ENDPOINTS". Every exported function converts to/
 * from the camelCase types in `types/index.ts` at the boundary, same
 * pattern as `consultationService.ts` / `patientService.ts`.
 *
 * Unlike the Prescription safety-check flow, the Lab endpoints' rejection
 * responses (403/404/409/422) are all plain `{ detail: string }` bodies --
 * no structured findings list to reconstruct -- so there's no need for
 * typed error classes here; callers use `extractErrorMessage` on whatever
 * axios error surfaces, same as the Patient module's simpler paths.
 */

interface LabSampleWire {
  collected_by: string | null;
  collected_at: string | null;
  processed_at: string | null;
  verified_by: string | null;
  verified_at: string | null;
  // Omitted entirely (not present as a key) for a nurse caller -- see
  // LabSample's doc comment in types/index.ts.
  result?: string | null;
  attached_to_emr_at: string | null;
}

interface LabOrderWire {
  id: string;
  consultation_id: string;
  patient_id: string;
  test_code: string;
  ordered_by: string;
  status: LabOrderStatus;
  created_at: string;
  sample: LabSampleWire | null;
}

export interface CreateLabOrderData {
  consultationId: string;
  testCode: string;
}

export interface ListLabOrdersParams {
  patientId?: string;
  consultationId?: string;
  status?: LabOrderStatus;
}

function toLabSample(wire: LabSampleWire): LabSample {
  const sample: LabSample = {
    collectedBy: wire.collected_by,
    collectedAt: wire.collected_at,
    processedAt: wire.processed_at,
    verifiedBy: wire.verified_by,
    verifiedAt: wire.verified_at,
    attachedToEmrAt: wire.attached_to_emr_at,
  };
  // Only assign the `result` key when the wire object actually has it, so a
  // nurse's response (which omits the key entirely) produces a `LabSample`
  // where `result` is genuinely `undefined` via absence -- same trick as
  // `patientService.ts`'s `toPatientFullRecord`.
  if ("result" in wire) {
    sample.result = wire.result ?? null;
  }
  return sample;
}

function toLabOrder(wire: LabOrderWire): LabOrder {
  return {
    id: wire.id,
    consultationId: wire.consultation_id,
    patientId: wire.patient_id,
    testCode: wire.test_code,
    orderedBy: wire.ordered_by,
    status: wire.status,
    createdAt: wire.created_at,
    sample: wire.sample ? toLabSample(wire.sample) : null,
  };
}

export async function createLabOrder(data: CreateLabOrderData): Promise<LabOrder> {
  const body = {
    consultation_id: data.consultationId,
    test_code: data.testCode,
  };
  const { data: wire } = await api.post<LabOrderWire>("/api/v1/lab/orders", body);
  return toLabOrder(wire);
}

export async function getLabOrder(id: string): Promise<LabOrder> {
  const { data: wire } = await api.get<LabOrderWire>(`/api/v1/lab/orders/${id}`);
  return toLabOrder(wire);
}

/**
 * Backend requires at least one of `patient_id`/`consultation_id`/`status`
 * (422 otherwise) -- see the PRP's ENDPOINTS table. This function doesn't
 * duplicate that guard client-side; both callers in this codebase
 * (`LabWorklistPage` always sends `status`) already satisfy it by
 * construction.
 */
export async function listLabOrders(params: ListLabOrdersParams): Promise<LabOrder[]> {
  const { data } = await api.get<LabOrderWire[]>("/api/v1/lab/orders", {
    params: {
      patient_id: params.patientId || undefined,
      consultation_id: params.consultationId || undefined,
      status: params.status || undefined,
    },
  });
  return data.map(toLabOrder);
}

/**
 * `result` is only meaningful for the `verified` transition -- the backend
 * rejects it being present for any other transition (422), so callers
 * simply don't pass it for `collected`/`processing`/`attached`.
 */
export async function transitionLabOrder(
  id: string,
  toStatus: Exclude<LabOrderStatus, "ordered">,
  result?: string,
): Promise<LabOrder> {
  const body = {
    to_status: toStatus,
    result: result ?? null,
  };
  const { data: wire } = await api.patch<LabOrderWire>(`/api/v1/lab/orders/${id}/transition`, body);
  return toLabOrder(wire);
}
