import { useEffect, useState } from "react";

import { listAppointments } from "../../services/appointmentService";
import type { AppointmentListItem } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface AppointmentSelectProps {
  value: string;
  onChange: (value: string) => void;
  branchId: string;
  departmentId?: number;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type an appointment UUID into a
 * text box" field (see `QueueBoardPage.tsx`'s check-in-by-appointment flow).
 * Re-fetches `GET /api/v1/appointments` (via `appointmentService.ts`)
 * whenever `branchId`/`departmentId` change -- same dependent-fetch shape as
 * `DoctorSelect`. Each option's label is `"{patientName} — {status}"` so a
 * user can tell appointments apart without seeing raw UUIDs.
 */
export default function AppointmentSelect({
  value,
  onChange,
  branchId,
  departmentId,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: AppointmentSelectProps): JSX.Element {
  const [appointments, setAppointments] = useState<AppointmentListItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!branchId) {
      setAppointments(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setAppointments(null);
    setLoadError(null);
    listAppointments({ branchId, departmentId })
      .then((found) => {
        if (!cancelled) {
          setAppointments(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load appointments."));
          setAppointments([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId, departmentId]);

  if (!branchId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a branch first…</option>
      </select>
    );
  }

  if (appointments === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (appointments.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No appointments found"}</option>
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
      <option value="">Select an appointment…</option>
      {appointments.map((appointment) => (
        <option key={appointment.id} value={appointment.id}>
          {appointment.patientName} — {appointment.status}
        </option>
      ))}
    </select>
  );
}
