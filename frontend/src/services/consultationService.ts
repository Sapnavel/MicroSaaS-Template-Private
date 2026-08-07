import axios from "axios";

import { api } from "./api";
import type {
  Consultation,
  ConsultationListItem,
  Diagnosis,
  PatientAllergy,
  Prescription,
  PrescriptionCreateResult,
  PrescriptionItem,
  SafetyFinding,
  SafetyTier,
} from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Clinical Consultation &
 * Prescription module -- see PRPs/clinical-consultation-prescription-prp.md
 * "ENDPOINTS". Every exported function converts to/from the camelCase
 * types in `types/index.ts` at the boundary, same pattern as
 * `patientService.ts` / `authService.ts`.
 */

interface DiagnosisWire {
  id: string;
  consultation_id: string;
  icd_code: string;
  description: string;
  is_primary: boolean;
}

interface PatientAllergyWire {
  id: string;
  patient_id: string;
  substance: string;
  severity: "mild" | "moderate" | "severe";
  reaction: string;
}

interface PrescriptionItemWire {
  id: string;
  drug_id: string;
  dosage: string;
  frequency: string;
  duration_days: number | null;
}

/**
 * Shape returned by GET /consultations/{id}/prescriptions and embedded in
 * the consultation-detail response -- each prescription carries its items.
 */
interface PrescriptionWire {
  id: string;
  consultation_id?: string;
  patient_id?: string;
  status: "draft" | "finalized" | "dispensed";
  created_at: string;
  items: PrescriptionItemWire[];
}

interface ConsultationWire {
  id: string;
  appointment_id: string;
  doctor_id: string;
  patient_id: string;
  symptoms: string;
  notes: string | null;
  started_at: string;
  ended_at: string | null;
  diagnoses: DiagnosisWire[];
  prescriptions: PrescriptionWire[];
}

/** `GET /api/v1/consultations?patient_id=` list-item shape -- backs
 * `components/selects/ConsultationSelect.tsx`. Distinct from
 * `ConsultationWire` above (a thin summary, not the full diagnoses/
 * prescriptions payload). */
interface ConsultationListItemWire {
  id: string;
  appointment_id: string;
  created_at: string;
  doctor_name: string;
}

interface SafetyFindingWire {
  source: "allergy" | "interaction";
  severity: string;
  tier: SafetyTier;
  message: string;
  drug_ids: string[];
}

interface PrescriptionCreateWire {
  prescription: {
    id: string;
    consultation_id: string;
    patient_id: string;
    status: "draft" | "finalized" | "dispensed";
    created_at: string;
  };
  items: PrescriptionItemWire[];
  safety_report: {
    allergy_findings: SafetyFindingWire[];
    interaction_findings: SafetyFindingWire[];
  };
}

interface BlockingFindingsWire {
  detail: string;
  blocking_findings: SafetyFindingWire[];
}

interface OverrideRequiredFindingsWire {
  detail: string;
  override_required_findings: SafetyFindingWire[];
}

export interface StartConsultationData {
  appointmentId: string;
  symptoms: string;
}

export interface AddDiagnosisData {
  icdCode: string;
  description: string;
  isPrimary: boolean;
}

export interface AddAllergyData {
  substance: string;
  severity: "mild" | "moderate" | "severe";
  reaction: string;
}

export interface PrescriptionItemInput {
  drugId: string;
  dosage: string;
  frequency: string;
  durationDays: number | null;
}

/**
 * Thrown on a 422 (`blocking_findings`) -- a HARD block. Per the PRP, there
 * is no override possible at any input for these; the only path forward is
 * removing the offending drug(s) from the request. Kept distinct from
 * `PrescriptionOverrideRequiredError` so `PrescriptionPage.tsx` can
 * `instanceof`-branch between "no override action exists" and "override is
 * available given a reason."
 */
export class PrescriptionBlockedError extends Error {
  blockingFindings: SafetyFinding[];

  constructor(message: string, blockingFindings: SafetyFinding[]) {
    super(message);
    this.name = "PrescriptionBlockedError";
    this.blockingFindings = blockingFindings;
  }
}

/**
 * Thrown on a 409 (`override_required_findings`) -- a SOFT block. Carries
 * the findings so the UI can display them and offer an explicit "override
 * and proceed anyway" action, which must resubmit with `override: true` and
 * a non-empty `overrideReason`.
 */
export class PrescriptionOverrideRequiredError extends Error {
  overrideRequiredFindings: SafetyFinding[];

  constructor(message: string, overrideRequiredFindings: SafetyFinding[]) {
    super(message);
    this.name = "PrescriptionOverrideRequiredError";
    this.overrideRequiredFindings = overrideRequiredFindings;
  }
}

function toDiagnosis(wire: DiagnosisWire): Diagnosis {
  return {
    id: wire.id,
    consultationId: wire.consultation_id,
    icdCode: wire.icd_code,
    description: wire.description,
    isPrimary: wire.is_primary,
  };
}

function toPatientAllergy(wire: PatientAllergyWire): PatientAllergy {
  return {
    id: wire.id,
    patientId: wire.patient_id,
    substance: wire.substance,
    severity: wire.severity,
    reaction: wire.reaction,
  };
}

function toPrescriptionItem(wire: PrescriptionItemWire): PrescriptionItem {
  return {
    id: wire.id,
    drugId: wire.drug_id,
    dosage: wire.dosage,
    frequency: wire.frequency,
    durationDays: wire.duration_days,
  };
}

function toPrescription(wire: PrescriptionWire, consultationId: string, patientId: string): Prescription {
  return {
    id: wire.id,
    consultationId: wire.consultation_id ?? consultationId,
    patientId: wire.patient_id ?? patientId,
    status: wire.status,
    createdAt: wire.created_at,
    items: wire.items.map(toPrescriptionItem),
  };
}

function toConsultation(wire: ConsultationWire): Consultation {
  return {
    id: wire.id,
    appointmentId: wire.appointment_id,
    doctorId: wire.doctor_id,
    patientId: wire.patient_id,
    symptoms: wire.symptoms,
    notes: wire.notes,
    startedAt: wire.started_at,
    endedAt: wire.ended_at,
    diagnoses: wire.diagnoses.map(toDiagnosis),
    prescriptions: wire.prescriptions.map((prescription) =>
      toPrescription(prescription, wire.id, wire.patient_id),
    ),
  };
}

function toConsultationListItem(wire: ConsultationListItemWire): ConsultationListItem {
  return {
    id: wire.id,
    appointmentId: wire.appointment_id,
    createdAt: wire.created_at,
    doctorName: wire.doctor_name,
  };
}

function toSafetyFinding(wire: SafetyFindingWire): SafetyFinding {
  return {
    source: wire.source,
    severity: wire.severity,
    tier: wire.tier,
    message: wire.message,
    drugIds: wire.drug_ids,
  };
}

/** `GET /api/v1/consultations?patient_id=` -- backs
 * `components/selects/ConsultationSelect.tsx`. */
export async function listConsultations(patientId: string): Promise<ConsultationListItem[]> {
  const { data } = await api.get<ConsultationListItemWire[]>("/api/v1/consultations", {
    params: { patient_id: patientId },
  });
  return data.map(toConsultationListItem);
}

export async function startConsultation(data: StartConsultationData): Promise<Consultation> {
  const body = {
    appointment_id: data.appointmentId,
    symptoms: data.symptoms,
  };
  const { data: wire } = await api.post<ConsultationWire>("/api/v1/consultations", body);
  return toConsultation(wire);
}

export async function getConsultation(id: string): Promise<Consultation> {
  const { data: wire } = await api.get<ConsultationWire>(`/api/v1/consultations/${id}`);
  return toConsultation(wire);
}

export async function completeConsultation(id: string, notes: string): Promise<Consultation> {
  const { data: wire } = await api.patch<ConsultationWire>(`/api/v1/consultations/${id}/complete`, {
    notes,
  });
  return toConsultation(wire);
}

export async function addDiagnosis(consultationId: string, data: AddDiagnosisData): Promise<Diagnosis> {
  const body = {
    icd_code: data.icdCode,
    description: data.description,
    is_primary: data.isPrimary,
  };
  const { data: wire } = await api.post<DiagnosisWire>(
    `/api/v1/consultations/${consultationId}/diagnoses`,
    body,
  );
  return toDiagnosis(wire);
}

export async function listAllergies(patientId: string): Promise<PatientAllergy[]> {
  const { data } = await api.get<PatientAllergyWire[]>(`/api/v1/patients/${patientId}/allergies`);
  return data.map(toPatientAllergy);
}

export async function addAllergy(patientId: string, data: AddAllergyData): Promise<PatientAllergy> {
  const { data: wire } = await api.post<PatientAllergyWire>(
    `/api/v1/patients/${patientId}/allergies`,
    data,
  );
  return toPatientAllergy(wire);
}

export async function createPrescription(
  consultationId: string,
  items: PrescriptionItemInput[],
  override = false,
  overrideReason?: string,
): Promise<PrescriptionCreateResult> {
  const body = {
    items: items.map((item) => ({
      drug_id: item.drugId,
      dosage: item.dosage,
      frequency: item.frequency,
      duration_days: item.durationDays,
    })),
    override,
    override_reason: overrideReason,
  };
  try {
    const { data: wire } = await api.post<PrescriptionCreateWire>(
      `/api/v1/consultations/${consultationId}/prescriptions`,
      body,
    );
    return {
      prescription: {
        id: wire.prescription.id,
        consultationId: wire.prescription.consultation_id,
        patientId: wire.prescription.patient_id,
        status: wire.prescription.status,
        createdAt: wire.prescription.created_at,
      },
      items: wire.items.map(toPrescriptionItem),
      safetyReport: {
        allergyFindings: wire.safety_report.allergy_findings.map(toSafetyFinding),
        interactionFindings: wire.safety_report.interaction_findings.map(toSafetyFinding),
      },
    };
  } catch (error) {
    if (axios.isAxiosError<BlockingFindingsWire>(error) && error.response?.status === 422) {
      const body422 = error.response.data;
      throw new PrescriptionBlockedError(
        body422.detail,
        (body422.blocking_findings ?? []).map(toSafetyFinding),
      );
    }
    if (axios.isAxiosError<OverrideRequiredFindingsWire>(error) && error.response?.status === 409) {
      const body409 = error.response.data;
      throw new PrescriptionOverrideRequiredError(
        body409.detail,
        (body409.override_required_findings ?? []).map(toSafetyFinding),
      );
    }
    throw error;
  }
}

/**
 * Summary shape returned by GET /consultations/{id}/prescriptions -- the
 * wire response for this endpoint omits `consultation_id`/`patient_id`
 * (they're implied by the URL), so this is `Prescription` minus those two
 * fields rather than the full type.
 */
export type PrescriptionSummary = Omit<Prescription, "consultationId" | "patientId">;

export async function listPrescriptions(consultationId: string): Promise<PrescriptionSummary[]> {
  const { data } = await api.get<PrescriptionWire[]>(
    `/api/v1/consultations/${consultationId}/prescriptions`,
  );
  return data.map((wire) => ({
    id: wire.id,
    status: wire.status,
    createdAt: wire.created_at,
    items: wire.items.map(toPrescriptionItem),
  }));
}
