import { api } from "./api";
import type { Doctor } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the doctor lookup endpoint. Converts
 * to/from the camelCase `Doctor` type in `types/index.ts` at the boundary,
 * same pattern as `branchService.ts`.
 *
 * Backs `components/selects/DoctorSelect.tsx`. `branchId` is required by the
 * backend (this list is always branch-scoped); `specialtyId` narrows it
 * further and is omitted from the query entirely when not supplied, same
 * "never send the key at all" convention `pharmacyService.ts` uses for its
 * optional `branchId`.
 */

interface DoctorWire {
  id: string;
  full_name: string;
  specialty_id: number;
  branch_id: string;
}

function toDoctor(wire: DoctorWire): Doctor {
  return {
    id: wire.id,
    fullName: wire.full_name,
    specialtyId: wire.specialty_id,
    branchId: wire.branch_id,
  };
}

export interface ListDoctorsParams {
  branchId: string;
  specialtyId?: number;
}

/** `GET /api/v1/doctors?branch_id=&specialty_id=(optional)`. */
export async function listDoctors(params: ListDoctorsParams): Promise<Doctor[]> {
  const query: Record<string, string | number> = { branch_id: params.branchId };
  if (params.specialtyId !== undefined) {
    query.specialty_id = params.specialtyId;
  }
  const { data } = await api.get<DoctorWire[]>("/api/v1/doctors", { params: query });
  return data.map(toDoctor);
}
