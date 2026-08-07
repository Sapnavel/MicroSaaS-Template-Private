import { useEffect, useState } from "react";

import { listStaff } from "../../services/staffService";
import type { Staff } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface StaffSelectProps {
  value: string;
  onChange: (value: string) => void;
  branchId: string;
  role?: string;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a staff-member UUID into a
 * text box" field. Re-fetches `GET /api/v1/staff` (via `staffService.ts`)
 * whenever `branchId`/`role` change -- same dependent-fetch shape as
 * `DoctorSelect`.
 */
export default function StaffSelect({
  value,
  onChange,
  branchId,
  role,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: StaffSelectProps): JSX.Element {
  const [staff, setStaff] = useState<Staff[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!branchId) {
      setStaff(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setStaff(null);
    setLoadError(null);
    listStaff({ branchId, role })
      .then((found) => {
        if (!cancelled) {
          setStaff(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load staff."));
          setStaff([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId, role]);

  if (!branchId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a branch first…</option>
      </select>
    );
  }

  if (staff === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (staff.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No staff found"}</option>
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
      <option value="">Select a staff member…</option>
      {staff.map((member) => (
        <option key={member.id} value={member.id}>
          {member.fullName}
        </option>
      ))}
    </select>
  );
}
