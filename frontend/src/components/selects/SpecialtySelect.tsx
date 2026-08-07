import { useEffect, useState } from "react";

import { listSpecialties } from "../../services/specialtyService";
import type { Specialty } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface SpecialtySelectProps {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a specialty ID into a text
 * box" field. Fetches `GET /api/v1/specialties` once on mount via
 * `specialtyService.ts` -- a flat, unscoped list, same shape as
 * `BranchSelect`.
 *
 * `Specialty.id` is a number on the wire/type, but this component's
 * `value`/`onChange` contract stays `string` (matching every other
 * `<XSelect>` here, and how a native `<select>`'s value always is) --
 * callers that need the numeric id parse it at the point of use (a
 * `<select>`'s value is always a string regardless of the underlying id
 * type).
 */
export default function SpecialtySelect({
  value,
  onChange,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: SpecialtySelectProps): JSX.Element {
  const [specialties, setSpecialties] = useState<Specialty[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listSpecialties()
      .then((found) => {
        if (!cancelled) {
          setSpecialties(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load specialties."));
          setSpecialties([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (specialties === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (specialties.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No specialties found"}</option>
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
      <option value="">Select a specialty…</option>
      {specialties.map((specialty) => (
        <option key={specialty.id} value={String(specialty.id)}>
          {specialty.name}
        </option>
      ))}
    </select>
  );
}
