import { api } from "./api";
import type { Specialty } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the specialty lookup endpoint.
 * Converts to/from the camelCase `Specialty` type in `types/index.ts` at the
 * boundary, same pattern as `branchService.ts`.
 *
 * Backs `components/selects/SpecialtySelect.tsx`, and `specialtyId` is also
 * an optional narrowing param `doctorService.ts#listDoctors` accepts.
 */

interface SpecialtyWire {
  id: number;
  name: string;
}

function toSpecialty(wire: SpecialtyWire): Specialty {
  return {
    id: wire.id,
    name: wire.name,
  };
}

/** `GET /api/v1/specialties` -- flat, unscoped list (no query params). */
export async function listSpecialties(): Promise<Specialty[]> {
  const { data } = await api.get<SpecialtyWire[]>("/api/v1/specialties");
  return data.map(toSpecialty);
}
