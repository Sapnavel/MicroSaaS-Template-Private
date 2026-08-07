import { api } from "./api";
import type { AppointmentListItem } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the appointment lookup/list
 * endpoint. Converts to/from the camelCase `AppointmentListItem` type in
 * `types/index.ts` at the boundary, same pattern as `doctorService.ts`.
 *
 * No `appointmentService.ts` existed before this change (booking-flow
 * appointment reads/writes, if any, live elsewhere) -- this file's sole
 * purpose is backing `components/selects/AppointmentSelect.tsx`'s dropdown.
 */

interface AppointmentListItemWire {
  id: string;
  patient_id: string;
  patient_name: string;
  doctor_id: string;
  room_id: string;
  status: string;
  is_emergency: boolean;
  triage_level: number | null;
}

function toAppointmentListItem(wire: AppointmentListItemWire): AppointmentListItem {
  return {
    id: wire.id,
    patientId: wire.patient_id,
    patientName: wire.patient_name,
    doctorId: wire.doctor_id,
    roomId: wire.room_id,
    status: wire.status,
    isEmergency: wire.is_emergency,
    triageLevel: wire.triage_level,
  };
}

export interface ListAppointmentsParams {
  branchId: string;
  departmentId?: number;
  date?: string;
}

/** `GET /api/v1/appointments?branch_id=&department_id=(optional)&date=(optional)`. */
export async function listAppointments(
  params: ListAppointmentsParams,
): Promise<AppointmentListItem[]> {
  const query: Record<string, string | number> = { branch_id: params.branchId };
  if (params.departmentId !== undefined) {
    query.department_id = params.departmentId;
  }
  if (params.date) {
    query.date = params.date;
  }
  const { data } = await api.get<AppointmentListItemWire[]>("/api/v1/appointments", { params: query });
  return data.map(toAppointmentListItem);
}
