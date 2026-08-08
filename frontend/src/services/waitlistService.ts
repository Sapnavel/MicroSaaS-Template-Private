import { api } from "./api";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for `/api/v1/waitlist`
 * (routers/waitlist.py, services/waitlist_service.py) -- HMS Project
 * Completion Prompt section 3.3, "Waiting list" / "Fair slot allocation".
 */

export type WaitlistEntryStatus = "waiting" | "offered" | "fulfilled" | "cancelled";

interface WaitlistEntryWire {
  id: string;
  branch_id: string;
  patient_id: string;
  doctor_id: string;
  requested_date: string;
  status: WaitlistEntryStatus;
  resolved_appointment_id: string | null;
  created_at: string;
}

export interface WaitlistEntry {
  id: string;
  branchId: string;
  patientId: string;
  doctorId: string;
  requestedDate: string;
  status: WaitlistEntryStatus;
  resolvedAppointmentId: string | null;
  createdAt: string;
}

export interface JoinWaitlistData {
  patientId: string;
  doctorId: string;
  requestedDate: string;
}

function toWaitlistEntry(wire: WaitlistEntryWire): WaitlistEntry {
  return {
    id: wire.id,
    branchId: wire.branch_id,
    patientId: wire.patient_id,
    doctorId: wire.doctor_id,
    requestedDate: wire.requested_date,
    status: wire.status,
    resolvedAppointmentId: wire.resolved_appointment_id,
    createdAt: wire.created_at,
  };
}

export async function joinWaitlist(data: JoinWaitlistData): Promise<WaitlistEntry> {
  const body = { patient_id: data.patientId, doctor_id: data.doctorId, requested_date: data.requestedDate };
  const { data: wire } = await api.post<WaitlistEntryWire>("/api/v1/waitlist", body);
  return toWaitlistEntry(wire);
}

/** Ordered oldest-first by the backend -- the same FIFO order the fairness
 * algorithm allocates freed slots in. */
export async function listWaitlist(doctorId?: string): Promise<WaitlistEntry[]> {
  const { data } = await api.get<WaitlistEntryWire[]>("/api/v1/waitlist", {
    params: { doctor_id: doctorId || undefined },
  });
  return data.map(toWaitlistEntry);
}

export async function fulfillWaitlistEntry(entryId: string, appointmentId: string): Promise<WaitlistEntry> {
  const { data: wire } = await api.post<WaitlistEntryWire>(`/api/v1/waitlist/${entryId}/fulfill`, {
    appointment_id: appointmentId,
  });
  return toWaitlistEntry(wire);
}

export async function cancelWaitlistEntry(entryId: string): Promise<void> {
  await api.delete<void>(`/api/v1/waitlist/${entryId}`);
}
