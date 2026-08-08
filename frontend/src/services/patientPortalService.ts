import axios from "axios";

import { api } from "./api";
import type { Appointment, Consultation, ConsultationListItem, Doctor, Invoice, InvoiceItem, InsuranceClaim, LabOrder, LabOrderStatus, LabSample, PatientFullRecord, TimelineEvent } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Patient Self-Service Portal
 * (`/api/v1/me/*`, routers/patient_portal.py). Wire shapes and converters
 * below deliberately mirror the staff-facing services (`patientService.ts`,
 * `appointmentService.ts`, `consultationService.ts`, `labService.ts`,
 * `billingService.ts`) field-for-field, since the backend reuses those same
 * response schemas -- see services/patient_portal_service.py's module
 * docstring for why (a patient viewing their own record sees the same
 * shape a doctor/nurse sees for it, not a parallel schema that could drift).
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

interface AppointmentWire {
  id: string;
  branch_id: string;
  patient_id: string;
  doctor_id: string;
  room_id: string;
  status: Appointment["status"];
  is_emergency: boolean;
  triage_level: number | null;
}

interface DoctorWire {
  id: string;
  full_name: string;
  specialty_id: number;
  branch_id: string;
}

interface ConsultationListItemWire {
  id: string;
  appointment_id: string;
  created_at: string;
  doctor_name: string;
}

interface DiagnosisWire {
  id: string;
  consultation_id: string;
  icd_code: string;
  description: string;
  is_primary: boolean;
}

interface PrescriptionItemWire {
  id: string;
  drug_id: string;
  dosage: string;
  frequency: string;
  duration_days: number | null;
}

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

interface LabSampleWire {
  collected_by: string | null;
  collected_at: string | null;
  processed_at: string | null;
  verified_by: string | null;
  verified_at: string | null;
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

/** `Decimal` fields serialize as JSON strings, not numbers -- see
 * `billingService.ts`'s `InvoiceWire` docstring for why (confirmed against
 * the live API); `toInvoice` below converts via `Number(...)`. */
interface InvoiceWire {
  id: string;
  patient_id: string;
  branch_id: string;
  status: Invoice["status"];
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
  source_type: InvoiceItem["sourceType"];
  source_id: string;
  description: string;
  // Decimal on the backend -- serializes as a JSON string, not a number.
  // See billingService.ts's InvoiceWire docstring for why.
  amount: string;
}

interface InsuranceClaimWire {
  id: string;
  invoice_id: string;
  payer_name: string;
  claim_amount: string;
  patient_copay: string;
  state: InsuranceClaim["state"];
  updated_at: string;
}

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

export interface CreateMyPatientData {
  fullName: string;
  dob: string;
  sex: string;
  phone: string;
  nationalId?: string;
  address?: string;
  allergiesNote?: string;
}

export interface BookMyAppointmentData {
  branchId: string;
  doctorId: string;
  startTime: string;
  durationMinutes: number;
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
  if ("national_id" in wire) record.nationalId = wire.national_id ?? null;
  if ("address" in wire) record.address = wire.address ?? null;
  if ("allergies_note" in wire) record.allergiesNote = wire.allergies_note ?? null;
  return record;
}

function toAppointment(wire: AppointmentWire): Appointment {
  return {
    id: wire.id,
    branchId: wire.branch_id,
    patientId: wire.patient_id,
    doctorId: wire.doctor_id,
    roomId: wire.room_id,
    status: wire.status,
    isEmergency: wire.is_emergency,
    triageLevel: wire.triage_level,
  };
}

function toDoctor(wire: DoctorWire): Doctor {
  return { id: wire.id, fullName: wire.full_name, specialtyId: wire.specialty_id, branchId: wire.branch_id };
}

function toConsultationListItem(wire: ConsultationListItemWire): ConsultationListItem {
  return { id: wire.id, appointmentId: wire.appointment_id, createdAt: wire.created_at, doctorName: wire.doctor_name };
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
    diagnoses: wire.diagnoses.map((d) => ({
      id: d.id,
      consultationId: d.consultation_id,
      icdCode: d.icd_code,
      description: d.description,
      isPrimary: d.is_primary,
    })),
    prescriptions: wire.prescriptions.map((p) => ({
      id: p.id,
      consultationId: p.consultation_id ?? wire.id,
      patientId: p.patient_id ?? wire.patient_id,
      status: p.status,
      createdAt: p.created_at,
      items: p.items.map((i) => ({ id: i.id, drugId: i.drug_id, dosage: i.dosage, frequency: i.frequency, durationDays: i.duration_days })),
    })),
  };
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
  if ("result" in wire) sample.result = wire.result ?? null;
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
  return { id: wire.id, invoiceId: wire.invoice_id, sourceType: wire.source_type, sourceId: wire.source_id, description: wire.description, amount: Number(wire.amount) };
}

function toInsuranceClaim(wire: InsuranceClaimWire): InsuranceClaim {
  return { id: wire.id, invoiceId: wire.invoice_id, payerName: wire.payer_name, claimAmount: Number(wire.claim_amount), patientCopay: Number(wire.patient_copay), state: wire.state, updatedAt: wire.updated_at };
}

// --- Profile -----------------------------------------------------------

/** `GET /api/v1/me/patient` -- `null` on a 404 (no profile linked yet),
 * rather than throwing, so callers can branch on presence without a
 * try/catch at every call site. */
export async function getMyPatient(): Promise<PatientFullRecord | null> {
  try {
    const { data } = await api.get<PatientWire>("/api/v1/me/patient");
    return toPatientFullRecord(data);
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      return null;
    }
    throw error;
  }
}

export async function createMyPatient(data: CreateMyPatientData): Promise<PatientFullRecord> {
  const body = {
    full_name: data.fullName,
    dob: data.dob,
    sex: data.sex,
    phone: data.phone,
    national_id: data.nationalId,
    address: data.address,
    allergies_note: data.allergiesNote,
  };
  const { data: wire } = await api.post<PatientWire>("/api/v1/me/patient", body);
  return toPatientFullRecord(wire);
}

// --- Doctor search -------------------------------------------------------

export async function searchMyDoctors(branchId?: string, specialtyId?: number): Promise<Doctor[]> {
  const { data } = await api.get<DoctorWire[]>("/api/v1/me/doctors", {
    params: { branch_id: branchId || undefined, specialty_id: specialtyId ?? undefined },
  });
  return data.map(toDoctor);
}

// --- Appointments --------------------------------------------------------

export async function listMyAppointments(): Promise<Appointment[]> {
  const { data } = await api.get<AppointmentWire[]>("/api/v1/me/appointments");
  return data.map(toAppointment);
}

export async function bookMyAppointment(data: BookMyAppointmentData): Promise<Appointment> {
  const body = {
    branch_id: data.branchId,
    doctor_id: data.doctorId,
    start_time: data.startTime,
    duration_minutes: data.durationMinutes,
  };
  const { data: wire } = await api.post<AppointmentWire>("/api/v1/me/appointments", body);
  return toAppointment(wire);
}

export async function cancelMyAppointment(appointmentId: string): Promise<void> {
  await api.delete<void>(`/api/v1/me/appointments/${appointmentId}`);
}

// --- Consultations ---------------------------------------------------------

export async function listMyConsultations(): Promise<ConsultationListItem[]> {
  const { data } = await api.get<ConsultationListItemWire[]>("/api/v1/me/consultations");
  return data.map(toConsultationListItem);
}

export async function getMyConsultation(id: string): Promise<Consultation> {
  const { data: wire } = await api.get<ConsultationWire>(`/api/v1/me/consultations/${id}`);
  return toConsultation(wire);
}

// --- Lab orders ------------------------------------------------------------

export async function listMyLabOrders(): Promise<LabOrder[]> {
  const { data } = await api.get<LabOrderWire[]>("/api/v1/me/lab-orders");
  return data.map(toLabOrder);
}

// --- Bills -------------------------------------------------------------------

export async function listMyBills(): Promise<Invoice[]> {
  const { data } = await api.get<InvoiceWire[]>("/api/v1/me/bills");
  return data.map(toInvoice);
}

export async function getMyBill(invoiceId: string): Promise<InvoiceDetail> {
  const { data: wire } = await api.get<InvoiceDetailWire>(`/api/v1/me/bills/${invoiceId}`);
  return {
    invoice: toInvoice(wire.invoice),
    items: wire.items.map(toInvoiceItem),
    claim: wire.claim ? toInsuranceClaim(wire.claim) : null,
  };
}

// --- Timeline (HMS Project Completion Prompt gap: "Patient medical
// timeline") -----------------------------------------------------------------

interface TimelineEventWire {
  event_type: string;
  id: string;
  occurred_at: string;
  summary: string;
}

function toTimelineEvent(wire: TimelineEventWire): TimelineEvent {
  return {
    eventType: wire.event_type,
    id: wire.id,
    occurredAt: wire.occurred_at,
    summary: wire.summary,
  };
}

export async function getMyTimeline(): Promise<TimelineEvent[]> {
  const { data } = await api.get<TimelineEventWire[]>("/api/v1/me/timeline");
  return data.map(toTimelineEvent);
}
