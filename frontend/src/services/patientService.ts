import axios from "axios";

import { api } from "./api";
import type { DuplicateCandidate, PatientDemographics, PatientFullRecord } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Patient Master Index module.
 * Every exported function here converts to/from the camelCase types in
 * `types/index.ts` at the boundary -- same pattern as `authService.ts`.
 */

interface PatientCreateWire {
  full_name: string;
  dob: string;
  sex: string;
  phone: string;
  national_id?: string;
  address?: string;
  allergies_note?: string;
  force?: boolean;
}

/**
 * Wire shape of a role-shaped patient response. The clinical fields are
 * optional on the wire type itself because the backend omits the keys
 * entirely for a front_desk caller -- see PatientFullRecord's doc comment
 * in types/index.ts.
 */
interface PatientWire {
  id: string;
  mrn: string;
  full_name: string;
  dob: string;
  sex: string;
  phone: string;
  user_id: string | null;
  merged_into_id: string | null;
  national_id?: string | null;
  address?: string | null;
  allergies_note?: string | null;
}

interface PatientConflictWire {
  detail: string;
  existing_patient_id: string;
  existing_patient_mrn: string;
}

interface DuplicateCandidateWire {
  id: string;
  patient_a: PatientDemographicsWire;
  patient_b: PatientDemographicsWire;
  match_score: number;
  match_reason: Record<string, number>;
  status: "pending" | "confirmed_merge" | "rejected";
  created_at: string;
}

interface PatientDemographicsWire {
  id: string;
  mrn: string;
  full_name: string;
  dob: string;
  sex: string;
  phone: string;
  user_id: string | null;
  merged_into_id: string | null;
}

interface MergeRequestWire {
  survivor_id: string;
}

interface MergeResponseWire {
  survivor: PatientWire;
  reassigned_appointments: number;
}

export interface MergeResult {
  survivor: PatientFullRecord;
  reassignedAppointments: number;
}

export interface CreatePatientData {
  fullName: string;
  dob: string;
  sex: string;
  phone: string;
  nationalId?: string;
  address?: string;
  allergiesNote?: string;
}

export interface SearchPatientsParams {
  name?: string;
  dob?: string;
  phone?: string;
}

/**
 * Thrown by `createPatient` on a 409 (deterministic dedup match found), so
 * `PatientRegisterPage` can branch on `instanceof` rather than re-parsing
 * the raw Axios error at the call site. Not specified verbatim by the
 * contract -- the contract only describes the 409 body shape -- but this
 * mirrors how `extractErrorMessage` already centralizes error handling.
 */
export class PatientConflictError extends Error {
  existingPatientId: string;
  existingPatientMrn: string;

  constructor(message: string, existingPatientId: string, existingPatientMrn: string) {
    super(message);
    this.name = "PatientConflictError";
    this.existingPatientId = existingPatientId;
    this.existingPatientMrn = existingPatientMrn;
  }
}

function toPatientDemographics(wire: PatientDemographicsWire): PatientDemographics {
  return {
    id: wire.id,
    mrn: wire.mrn,
    fullName: wire.full_name,
    dob: wire.dob,
    sex: wire.sex,
    phone: wire.phone,
    userId: wire.user_id,
    mergedIntoId: wire.merged_into_id,
  };
}

function toPatientFullRecord(wire: PatientWire): PatientFullRecord {
  const record: PatientFullRecord = {
    id: wire.id,
    mrn: wire.mrn,
    fullName: wire.full_name,
    dob: wire.dob,
    sex: wire.sex,
    phone: wire.phone,
    userId: wire.user_id,
    mergedIntoId: wire.merged_into_id,
  };
  // Only assign clinical keys when the wire object actually has them, so a
  // front_desk response (which omits these keys entirely) produces a
  // PatientFullRecord where the keys are genuinely `undefined` via absence,
  // not present-but-set-to-undefined.
  if ("national_id" in wire) {
    record.nationalId = wire.national_id ?? null;
  }
  if ("address" in wire) {
    record.address = wire.address ?? null;
  }
  if ("allergies_note" in wire) {
    record.allergiesNote = wire.allergies_note ?? null;
  }
  return record;
}

function toDuplicateCandidate(wire: DuplicateCandidateWire): DuplicateCandidate {
  return {
    id: wire.id,
    patientA: toPatientDemographics(wire.patient_a),
    patientB: toPatientDemographics(wire.patient_b),
    matchScore: wire.match_score,
    matchReason: wire.match_reason,
    status: wire.status,
    createdAt: wire.created_at,
  };
}

export async function createPatient(
  data: CreatePatientData,
  force = false,
): Promise<PatientFullRecord> {
  const body: PatientCreateWire = {
    full_name: data.fullName,
    dob: data.dob,
    sex: data.sex,
    phone: data.phone,
    national_id: data.nationalId,
    address: data.address,
    allergies_note: data.allergiesNote,
    force,
  };
  try {
    const { data: wire } = await api.post<PatientWire>("/api/v1/patients", body);
    return toPatientFullRecord(wire);
  } catch (error) {
    if (axios.isAxiosError<PatientConflictWire>(error) && error.response?.status === 409) {
      const conflict = error.response.data;
      throw new PatientConflictError(
        conflict.detail,
        conflict.existing_patient_id,
        conflict.existing_patient_mrn,
      );
    }
    throw error;
  }
}

export async function searchPatients(params: SearchPatientsParams): Promise<PatientFullRecord[]> {
  const { data } = await api.get<PatientWire[]>("/api/v1/patients/search", {
    params: {
      name: params.name || undefined,
      dob: params.dob || undefined,
      phone: params.phone || undefined,
    },
  });
  return data.map(toPatientFullRecord);
}

export async function getPatient(id: string): Promise<PatientFullRecord> {
  const { data } = await api.get<PatientWire>(`/api/v1/patients/${id}`);
  return toPatientFullRecord(data);
}

export async function listDuplicates(): Promise<DuplicateCandidate[]> {
  const { data } = await api.get<DuplicateCandidateWire[]>("/api/v1/patients/duplicates");
  return data.map(toDuplicateCandidate);
}

export async function mergeDuplicate(candidateId: string, survivorId: string): Promise<MergeResult> {
  const body: MergeRequestWire = { survivor_id: survivorId };
  const { data } = await api.post<MergeResponseWire>(
    `/api/v1/patients/duplicates/${candidateId}/merge`,
    body,
  );
  return {
    survivor: toPatientFullRecord(data.survivor),
    reassignedAppointments: data.reassigned_appointments,
  };
}

export async function rejectDuplicate(candidateId: string): Promise<void> {
  await api.post<void>(`/api/v1/patients/duplicates/${candidateId}/reject`);
}
