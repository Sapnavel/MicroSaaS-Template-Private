import { useEffect, useState } from "react";

import { listConsultations } from "../../services/consultationService";
import type { ConsultationListItem } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface ConsultationSelectProps {
  value: string;
  onChange: (value: string) => void;
  patientId: string;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a consultation UUID into a
 * text box" field. Re-fetches `GET /api/v1/consultations?patient_id=` (via
 * `consultationService.ts#listConsultations`) whenever `patientId` changes;
 * disabled/empty while `patientId` is blank -- same dependent-fetch shape as
 * `DoctorSelect`, keyed off a patient instead of a branch. Each option's
 * label is the consultation's date (from `createdAt`) plus `doctorName` so
 * a user can tell consultations apart without seeing raw UUIDs.
 */
export default function ConsultationSelect({
  value,
  onChange,
  patientId,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: ConsultationSelectProps): JSX.Element {
  const [consultations, setConsultations] = useState<ConsultationListItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!patientId) {
      setConsultations(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setConsultations(null);
    setLoadError(null);
    listConsultations(patientId)
      .then((found) => {
        if (!cancelled) {
          setConsultations(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load consultations."));
          setConsultations([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [patientId]);

  if (!patientId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a patient first…</option>
      </select>
    );
  }

  if (consultations === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (consultations.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No consultations found"}</option>
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
      <option value="">Select a consultation…</option>
      {consultations.map((consultation) => (
        <option key={consultation.id} value={consultation.id}>
          {new Date(consultation.createdAt).toLocaleDateString()} — {consultation.doctorName}
        </option>
      ))}
    </select>
  );
}
