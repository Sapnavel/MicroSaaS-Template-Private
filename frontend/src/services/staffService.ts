import { api } from "./api";
import type { Staff } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the staff lookup endpoint. Converts
 * to/from the camelCase `Staff` type in `types/index.ts` at the boundary,
 * same pattern as `doctorService.ts`.
 *
 * Backs `components/selects/StaffSelect.tsx`. `branchId` is required by the
 * backend (this list is always branch-scoped); `role` narrows it further
 * and is omitted from the query entirely when not supplied. `branchId` on
 * the wire item itself is nullable (some staff, e.g. `system_admin`, have
 * no home branch) -- distinct from the request-level `branchId`, which is
 * always required here.
 */

interface StaffWire {
  id: string;
  full_name: string;
  role: string;
  branch_id: string | null;
}

function toStaff(wire: StaffWire): Staff {
  return {
    id: wire.id,
    fullName: wire.full_name,
    role: wire.role,
    branchId: wire.branch_id,
  };
}

export interface ListStaffParams {
  branchId: string;
  role?: string;
}

/** `GET /api/v1/staff?branch_id=&role=(optional)`. */
export async function listStaff(params: ListStaffParams): Promise<Staff[]> {
  const query: Record<string, string> = { branch_id: params.branchId };
  if (params.role) {
    query.role = params.role;
  }
  const { data } = await api.get<StaffWire[]>("/api/v1/staff", { params: query });
  return data.map(toStaff);
}
