import { useEffect, useState } from "react";

import { listClaims } from "../../services/billingService";
import type { ClaimListItem, ClaimState } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface ClaimSelectProps {
  value: string;
  onChange: (value: string) => void;
  branchId: string;
  state?: ClaimState;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a claim UUID into a text
 * box" field (see `ClaimsPage.tsx`). Re-fetches `GET /api/v1/billing/claims`
 * (via `billingService.ts#listClaims`) whenever `branchId`/`state` change --
 * same dependent-fetch shape as `DoctorSelect`. Each option's label is
 * `"{patientName} — {state}"` so a user can tell claims apart without
 * seeing raw UUIDs.
 */
export default function ClaimSelect({
  value,
  onChange,
  branchId,
  state,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: ClaimSelectProps): JSX.Element {
  const [claims, setClaims] = useState<ClaimListItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!branchId) {
      setClaims(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setClaims(null);
    setLoadError(null);
    listClaims({ branchId, state })
      .then((found) => {
        if (!cancelled) {
          setClaims(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load claims."));
          setClaims([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId, state]);

  if (!branchId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a branch first…</option>
      </select>
    );
  }

  if (claims === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (claims.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No claims found"}</option>
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
      <option value="">Select a claim…</option>
      {claims.map((claim) => (
        <option key={claim.id} value={claim.id}>
          {claim.patientName} — {claim.state}
        </option>
      ))}
    </select>
  );
}
