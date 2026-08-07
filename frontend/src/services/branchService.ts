import { api } from "./api";
import type { Branch } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the branch lookup endpoint. Converts
 * to/from the camelCase `Branch` type in `types/index.ts` at the boundary,
 * same pattern as `queueService.ts`.
 *
 * Backs `components/selects/BranchSelect.tsx` -- the reusable dropdown
 * replacement for the "type a branch UUID into a text box" fields scattered
 * across the app (see e.g. `BedMatrixPage.tsx`'s free-text branch-ID input).
 */

interface BranchWire {
  id: string;
  name: string;
}

function toBranch(wire: BranchWire): Branch {
  return {
    id: wire.id,
    name: wire.name,
  };
}

/** `GET /api/v1/branches` -- flat, unscoped list (no query params). */
export async function listBranches(): Promise<Branch[]> {
  const { data } = await api.get<BranchWire[]>("/api/v1/branches");
  return data.map(toBranch);
}
