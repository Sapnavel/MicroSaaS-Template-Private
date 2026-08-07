import { useEffect, useState } from "react";

import { listWards } from "../../services/wardService";
import type { Ward } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface WardSelectProps {
  value: string;
  onChange: (value: string) => void;
  branchId: string;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a ward UUID into a text
 * box" field (see `BedMatrixPage.tsx`'s free-text ward-ID filter). Re-fetches
 * `GET /api/v1/wards` (via `wardService.ts#listWards`) whenever `branchId`
 * changes -- same dependent-fetch shape as `DoctorSelect`/`RoomSelect`.
 */
export default function WardSelect({
  value,
  onChange,
  branchId,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: WardSelectProps): JSX.Element {
  const [wards, setWards] = useState<Ward[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!branchId) {
      setWards(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setWards(null);
    setLoadError(null);
    listWards(branchId)
      .then((found) => {
        if (!cancelled) {
          setWards(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load wards."));
          setWards([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId]);

  if (!branchId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a branch first…</option>
      </select>
    );
  }

  if (wards === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (wards.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No wards found"}</option>
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
      <option value="">Select a ward…</option>
      {wards.map((ward) => (
        <option key={ward.id} value={ward.id}>
          {ward.name}
        </option>
      ))}
    </select>
  );
}
