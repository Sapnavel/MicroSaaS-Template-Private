import { api } from "./api";
import type { Drug } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the drug-catalog lookup endpoint.
 * Converts to/from the camelCase `Drug` type in `types/index.ts` at the
 * boundary, same pattern as `branchService.ts`.
 *
 * Deliberately a separate file from `pharmacyService.ts`: that file owns
 * per-branch *inventory* (`InventoryItem`/`InventoryBatch`, keyed by an
 * already-known `drugId`), not the drug catalog itself -- this is the
 * catalog lookup backing `components/selects/DrugSelect.tsx`'s live-filter
 * typeahead (~294 drugs, too many for a plain `<select>`).
 */

interface DrugWire {
  id: string;
  name: string;
  generic_name: string | null;
}

function toDrug(wire: DrugWire): Drug {
  return {
    id: wire.id,
    name: wire.name,
    genericName: wire.generic_name,
  };
}

export interface ListDrugsParams {
  query?: string;
}

/** `GET /api/v1/drugs?query=(optional)`. */
export async function listDrugs(params: ListDrugsParams = {}): Promise<Drug[]> {
  const { data } = await api.get<DrugWire[]>("/api/v1/drugs", {
    params: params.query ? { query: params.query } : undefined,
  });
  return data.map(toDrug);
}
