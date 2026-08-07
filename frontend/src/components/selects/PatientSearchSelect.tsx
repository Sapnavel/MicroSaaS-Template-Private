import { useEffect, useState } from "react";

import { searchPatients } from "../../services/patientService";
import type { PatientFullRecord } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

const DEBOUNCE_MS = 300;
const MIN_QUERY_LENGTH = 2;

export interface PatientSearchSelectProps {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  className?: string;
  disabled?: boolean;
}

/**
 * Reusable replacement for a raw "type a patient UUID into a text box"
 * field. Patients are unbounded (there's no "list all patients" endpoint,
 * nor should there be), so unlike every other `<XSelect>` here this is an
 * async typeahead against the existing `GET /api/v1/patients/search` (via
 * `patientService.ts#searchPatients`, reused as-is -- no new endpoint was
 * added for this), not a scoped dropdown.
 *
 * Debounced 300ms, only fires once at least 2 characters are typed.
 * `PatientSearchPage.tsx` (the only existing caller of this endpoint)
 * doesn't itself enforce a minimum length beyond "at least one of
 * name/dob/phone is non-empty" -- the 2-character minimum here is this
 * component's own, documented choice, so a search doesn't fire on every
 * single first keystroke.
 *
 * Once a patient is picked, this shows a small fixed "value bar" with their
 * name and MRN plus a "change" link, instead of an inline editable text
 * field the user could accidentally overtype -- clicking "change" clears
 * the selection and re-shows the search input. If a caller passes a
 * non-empty `value` this component didn't itself just set, it starts back
 * at the search input (there's no "look up one patient by id" round trip
 * wired up here) -- same documented simplification `DrugSelect` makes.
 */
export default function PatientSearchSelect({
  value,
  onChange,
  id,
  className = "auth-input",
  disabled = false,
}: PatientSearchSelectProps): JSX.Element {
  const [query, setQuery] = useState<string>("");
  const [selected, setSelected] = useState<PatientFullRecord | null>(null);
  const [results, setResults] = useState<PatientFullRecord[] | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (query.trim().length < MIN_QUERY_LENGTH) {
      setResults(null);
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    const timeoutId = window.setTimeout(() => {
      searchPatients({ name: query.trim() })
        .then((found) => {
          setResults(found);
        })
        .catch((err: unknown) => {
          setError(extractErrorMessage(err, "Could not search patients."));
          setResults([]);
        })
        .finally(() => {
          setIsLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timeoutId);
  }, [query]);

  const handlePick = (patient: PatientFullRecord): void => {
    setSelected(patient);
    setQuery("");
    setResults(null);
    onChange(patient.id);
  };

  const handleChange = (): void => {
    setSelected(null);
    onChange("");
  };

  if (value && selected) {
    return (
      <div id={id} className="select-search-value">
        <span>
          {selected.fullName} <span className="field-hint">MRN {selected.mrn}</span>
        </span>
        <button
          type="button"
          className="select-search-change"
          onClick={handleChange}
          disabled={disabled}
        >
          Change
        </button>
      </div>
    );
  }

  return (
    <div className="select-search">
      <input
        id={id}
        className={className}
        type="text"
        placeholder="Type a patient's name to search…"
        value={query}
        disabled={disabled}
        onChange={(event) => setQuery(event.target.value)}
      />
      {isLoading && <p className="field-hint">Searching…</p>}
      {error !== null && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      {results !== null && (
        <>
          {results.length === 0 ? (
            <p className="empty-state">No patients found.</p>
          ) : (
            <ul className="select-search-results">
              {results.map((patient) => (
                <li key={patient.id}>
                  <button
                    type="button"
                    className="select-search-result"
                    onClick={() => handlePick(patient)}
                  >
                    {patient.fullName} — MRN {patient.mrn}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
