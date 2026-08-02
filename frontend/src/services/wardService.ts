import axios from "axios";

import { api } from "./api";
import type { Admission, BedMatrixEntry, BedStatus, OTSchedule } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Ward, Bed & OT module -- see
 * PRPs/ward-bed-ot-module-prp.md "ENDPOINTS". Every exported function
 * converts to/from the camelCase types in `types/index.ts` at the boundary,
 * same pattern as `pharmacyService.ts` / `labService.ts` /
 * `consultationService.ts`.
 *
 * `branchId` handling mirrors `pharmacyService.ts`: `GET /wards/beds` is
 * "branch-scoped like Pharmacy" per the PRP's ENDPOINTS section, so a
 * caller with their own branch (nurse/doctor/front_desk) simply never
 * sends `branch_id` (the backend derives it from the session), and only a
 * `system_admin` (whose `branchId` is `null`) supplies it explicitly.
 */

interface BedMatrixEntryWire {
  id: string;
  ward_id: string;
  ward_name: string;
  branch_id: string;
  label: string;
  status: BedStatus;
}

interface AdmissionWire {
  id: string;
  patient_id: string;
  bed_id: string;
  admitted_by: string;
  start_time: string;
  end_time: string | null;
  discharged_at: string | null;
}

interface OTScheduleWire {
  id: string;
  room_id: string;
  patient_id: string;
  surgeon_id: string;
  start_time: string;
  end_time: string;
}

/** Shape of the 200 body from `PATCH /wards/beds/{id}/status` -- a partial
 * bed, not the full `BedMatrixEntry` (no `ward_name`/`branch_id`). */
interface BedStatusWire {
  id: string;
  ward_id: string;
  label: string;
  status: BedStatus;
}

/** Shape of the 409 body from `PATCH /wards/beds/{id}/status`. */
interface BedStatusConflictWire {
  detail: string;
  current: BedStatus;
  requested: BedStatus;
}

export type OTConflictReason = "surgeon_busy_ot" | "surgeon_busy_appointment" | "room_conflict";

/** Shape of the 409 body from `POST /wards/ot-schedule`. */
interface OTConflictWire {
  detail: string;
  reason: OTConflictReason;
}

export interface GetBedMatrixParams {
  branchId?: string;
  wardId?: string;
  status?: BedStatus;
}

export interface AdmitPatientData {
  patientId: string;
  bedId: string;
  startTime: string;
}

export interface ScheduleOTData {
  roomId: string;
  patientId: string;
  surgeonId: string;
  startTime: string;
  durationMinutes: number;
}

export interface ListOTSchedulesParams {
  roomId?: string;
  surgeonId?: string;
  fromTime?: string;
  toTime?: string;
}

/** Partial bed shape returned by `updateBedStatus` -- see `BedStatusWire`. */
export interface BedStatusResult {
  id: string;
  wardId: string;
  label: string;
  status: BedStatus;
}

/**
 * Thrown on a 409 from `updateBedStatus()` -- the requested manual
 * transition isn't one of the four legal ones (see the PRP's
 * `set_bed_status` design). Carries the backend's actual `current`/
 * `requested` status strings so the UI can say e.g. "cannot go from
 * occupied to available" instead of a generic "invalid transition".
 */
export class IllegalBedStatusTransitionError extends Error {
  current: BedStatus;
  requested: BedStatus;

  constructor(message: string, current: BedStatus, requested: BedStatus) {
    super(message);
    this.name = "IllegalBedStatusTransitionError";
    this.current = current;
    this.requested = requested;
  }
}

/**
 * Thrown on a 409 from `scheduleOT()`. Carries the backend's `reason` so
 * the UI can distinguish "surgeon busy in another OT" vs. "surgeon has a
 * conflicting clinic appointment" vs. "room already booked" -- see the
 * PRP's design decision #3 (the surgeon-conflict check is app-level only,
 * spanning both `ot_schedules` and `appointments`).
 */
export class OTConflictError extends Error {
  reason: OTConflictReason;

  constructor(message: string, reason: OTConflictReason) {
    super(message);
    this.name = "OTConflictError";
    this.reason = reason;
  }
}

function toBedMatrixEntry(wire: BedMatrixEntryWire): BedMatrixEntry {
  return {
    id: wire.id,
    wardId: wire.ward_id,
    wardName: wire.ward_name,
    branchId: wire.branch_id,
    label: wire.label,
    status: wire.status,
  };
}

function toAdmission(wire: AdmissionWire): Admission {
  return {
    id: wire.id,
    patientId: wire.patient_id,
    bedId: wire.bed_id,
    admittedBy: wire.admitted_by,
    startTime: wire.start_time,
    endTime: wire.end_time,
    dischargedAt: wire.discharged_at,
  };
}

function toOTSchedule(wire: OTScheduleWire): OTSchedule {
  return {
    id: wire.id,
    roomId: wire.room_id,
    patientId: wire.patient_id,
    surgeonId: wire.surgeon_id,
    startTime: wire.start_time,
    endTime: wire.end_time,
  };
}

export async function getBedMatrix(params: GetBedMatrixParams = {}): Promise<BedMatrixEntry[]> {
  const query: Record<string, string> = {};
  if (params.branchId) {
    query.branch_id = params.branchId;
  }
  if (params.wardId) {
    query.ward_id = params.wardId;
  }
  if (params.status) {
    query.status = params.status;
  }
  const { data } = await api.get<BedMatrixEntryWire[]>("/api/v1/wards/beds", {
    params: Object.keys(query).length > 0 ? query : undefined,
  });
  return data.map(toBedMatrixEntry);
}

export async function admitPatient(data: AdmitPatientData): Promise<Admission> {
  const body = {
    patient_id: data.patientId,
    bed_id: data.bedId,
    start_time: data.startTime,
  };
  const { data: wire } = await api.post<AdmissionWire>("/api/v1/wards/admissions", body);
  return toAdmission(wire);
}

/** `dischargeTime` is optional per the PRP's ENDPOINTS table -- omitted
 * entirely from the body (not sent as `null`) when not supplied, letting
 * the backend default it to "now". */
export async function dischargePatient(admissionId: string, dischargeTime?: string): Promise<Admission> {
  const body = dischargeTime ? { discharge_time: dischargeTime } : {};
  const { data: wire } = await api.patch<AdmissionWire>(
    `/api/v1/wards/admissions/${admissionId}/discharge`,
    body,
  );
  return toAdmission(wire);
}

/** Returns the NEW admission (a new row, not an update of the old one) --
 * see the PRP's `transfer_patient` design (discharge-then-admit). */
export async function transferPatient(admissionId: string, newBedId: string): Promise<Admission> {
  const body = { new_bed_id: newBedId };
  const { data: wire } = await api.patch<AdmissionWire>(
    `/api/v1/wards/admissions/${admissionId}/transfer`,
    body,
  );
  return toAdmission(wire);
}

/**
 * Throws `IllegalBedStatusTransitionError` on a 409 -- callers should
 * `instanceof`-branch on it before falling back to `extractErrorMessage`,
 * same pattern as `pharmacyService.ts`'s `dispense()`.
 */
export async function updateBedStatus(
  bedId: string,
  status: Extract<BedStatus, "available" | "blocked">,
): Promise<BedStatusResult> {
  try {
    const { data: wire } = await api.patch<BedStatusWire>(`/api/v1/wards/beds/${bedId}/status`, {
      status,
    });
    return { id: wire.id, wardId: wire.ward_id, label: wire.label, status: wire.status };
  } catch (error) {
    if (axios.isAxiosError<BedStatusConflictWire>(error) && error.response?.status === 409) {
      const body409 = error.response.data;
      throw new IllegalBedStatusTransitionError(body409.detail, body409.current, body409.requested);
    }
    throw error;
  }
}

/**
 * Throws `OTConflictError` on a 409 -- callers should `instanceof`-branch
 * on it before falling back to `extractErrorMessage`.
 */
export async function scheduleOT(data: ScheduleOTData): Promise<OTSchedule> {
  const body = {
    room_id: data.roomId,
    patient_id: data.patientId,
    surgeon_id: data.surgeonId,
    start_time: data.startTime,
    duration_minutes: data.durationMinutes,
  };
  try {
    const { data: wire } = await api.post<OTScheduleWire>("/api/v1/wards/ot-schedule", body);
    return toOTSchedule(wire);
  } catch (error) {
    if (axios.isAxiosError<OTConflictWire>(error) && error.response?.status === 409) {
      const body409 = error.response.data;
      throw new OTConflictError(body409.detail, body409.reason);
    }
    throw error;
  }
}

export async function listOTSchedules(params: ListOTSchedulesParams = {}): Promise<OTSchedule[]> {
  const query: Record<string, string> = {};
  if (params.roomId) {
    query.room_id = params.roomId;
  }
  if (params.surgeonId) {
    query.surgeon_id = params.surgeonId;
  }
  if (params.fromTime) {
    query.from_time = params.fromTime;
  }
  if (params.toTime) {
    query.to_time = params.toTime;
  }
  const { data } = await api.get<OTScheduleWire[]>("/api/v1/wards/ot-schedule", {
    params: Object.keys(query).length > 0 ? query : undefined,
  });
  return data.map(toOTSchedule);
}
