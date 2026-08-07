import { useEffect, useState } from "react";

import { listDoctors } from "../../services/doctorService";
import type { Doctor } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface DoctorSelectProps {
  value: string;
  onChange: (value: string) => void;
  branchId: string;
  specialtyId?: number;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a doctor UUID into a text
 * box" field. Re-fetches `GET /api/v1/doctors` (via `doctorService.ts`)
 * whenever `branchId`/`specialtyId` change. `branchId` is required by the
 * backend, so while it's empty/not yet chosen this renders disabled with an
 * explanatory placeholder rather than firing a request that would 422.
 */
export default function DoctorSelect({
  value,
  onChange,
  branchId,
  specialtyId,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: DoctorSelectProps): JSX.Element {
  const [doctors, setDoctors] = useState<Doctor[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!branchId) {
      setDoctors(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setDoctors(null);
    setLoadError(null);
    listDoctors({ branchId, specialtyId })
      .then((found) => {
        if (!cancelled) {
          setDoctors(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load doctors."));
          setDoctors([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId, specialtyId]);

  if (!branchId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a branch first…</option>
      </select>
    );
  }

  if (doctors === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (doctors.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No doctors found"}</option>
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
      <option value="">Select a doctor…</option>
      {doctors.map((doctor) => (
        <option key={doctor.id} value={doctor.id}>
          {doctor.fullName}
        </option>
      ))}
    </select>
  );
}
