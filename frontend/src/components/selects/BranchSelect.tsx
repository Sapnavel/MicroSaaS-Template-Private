import { useEffect, useState } from "react";

import { listBranches } from "../../services/branchService";
import type { Branch } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface BranchSelectProps {
  value: string;
  onChange: (value: string) => void;
  /** Prepends an "All branches" option with an empty-string value --
   * `ExecutiveDashboardPage` needs this, since a blank `branch_id` there
   * means "aggregate across all branches", not "nothing chosen yet". */
  allowAll?: boolean;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a branch UUID into a text
 * box" field (see e.g. `BedMatrixPage.tsx`'s free-text branch-ID input this
 * is meant to eventually replace). Fetches `GET /api/v1/branches` once on
 * mount via `branchService.ts` -- a flat, unscoped list, so there is no
 * re-fetch dependency here unlike `DoctorSelect`/`RoomSelect`/etc.
 */
export default function BranchSelect({
  value,
  onChange,
  allowAll = false,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: BranchSelectProps): JSX.Element {
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listBranches()
      .then((found) => {
        if (!cancelled) {
          setBranches(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load branches."));
          setBranches([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (branches === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (branches.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No branches found"}</option>
      </select>
    );
  }

  return (
    <select
      id={id}
      className={className}
      value={value}
      disabled={disabled}
      required={required}
      onChange={(event) => onChange(event.target.value)}
    >
      {allowAll ? (
        <option value="">All branches</option>
      ) : (
        <option value="">Select a branch…</option>
      )}
      {branches.map((branch) => (
        <option key={branch.id} value={branch.id}>
          {branch.name}
        </option>
      ))}
    </select>
  );
}
