import { useEffect, useState } from "react";

import { listDrugs } from "../../services/drugService";
import type { Drug } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

const DEBOUNCE_MS = 250;

export interface DrugSelectProps {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  className?: string;
  disabled?: boolean;
}

/**
 * Reusable replacement for a raw "type a drug UUID into a text box" field
 * (see `PrescriptionPage.tsx`/`PharmacyDispensePage.tsx`/
 * `PharmacyInventoryPage.tsx`'s `drug_id` inputs). There are ~294 drugs in
 * this system -- too many for a plain `<select>` -- so this is a debounced
 * (250ms) live-filter text input (calls `GET /api/v1/drugs?query=` via
 * `drugService.ts`) plus a plain clickable results list below it, same
 * lightweight pattern as `PatientSearchSelect` (no headless combobox
 * library, per FRONTEND-AGENT instructions).
 *
 * Exposes the same `value`/`onChange` contract as every other `<XSelect>`
 * here (`value` is the picked drug's id), but since a bare id can't be
 * turned back into a display name without another round trip, this
 * component tracks the picked drug's name in its own `selectedLabel` state
 * -- set at the moment of picking, not derived from `value` -- and shows
 * that in the input instead of the id. If a caller passes in a non-empty
 * `value` this component didn't itself just set (e.g. a value pre-filled
 * from elsewhere), the input simply starts blank/searchable again rather
 * than guessing at a label -- a deliberate, documented simplification given
 * the "keep this simple" scope.
 */
export default function DrugSelect({
  value,
  onChange,
  id,
  className = "auth-input",
  disabled = false,
}: DrugSelectProps): JSX.Element {
  const [query, setQuery] = useState<string>("");
  const [selectedLabel, setSelectedLabel] = useState<string | null>(null);
  const [results, setResults] = useState<Drug[] | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (value && selectedLabel) {
      // Already showing the picked label -- nothing to search for.
      return;
    }
    if (!query.trim()) {
      setResults(null);
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    const timeoutId = window.setTimeout(() => {
      listDrugs({ query: query.trim() })
        .then((found) => {
          setResults(found);
        })
        .catch((err: unknown) => {
          setError(extractErrorMessage(err, "Could not search drugs."));
          setResults([]);
        })
        .finally(() => {
          setIsLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timeoutId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const handleInputChange = (text: string): void => {
    setQuery(text);
    setSelectedLabel(null);
    setResults(null);
    onChange("");
  };

  const handlePick = (drug: Drug): void => {
    setSelectedLabel(drug.genericName ? `${drug.name} (${drug.genericName})` : drug.name);
    setQuery("");
    setResults(null);
    onChange(drug.id);
  };

  const displayValue = value && selectedLabel ? selectedLabel : query;

  return (
    <div className="select-search">
      <input
        id={id}
        className={className}
        type="text"
        placeholder="Type to search drugs…"
        value={displayValue}
        disabled={disabled}
        onChange={(event) => handleInputChange(event.target.value)}
      />
      {isLoading && <p className="field-hint">Searching…</p>}
      {error !== null && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      {results !== null && !(value && selectedLabel) && (
        <>
          {results.length === 0 ? (
            <p className="empty-state">No drugs found.</p>
          ) : (
            <ul className="select-search-results">
              {results.map((drug) => (
                <li key={drug.id}>
                  <button type="button" className="select-search-result" onClick={() => handlePick(drug)}>
                    {drug.name}
                    {drug.genericName ? ` (${drug.genericName})` : ""}
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
