import { useState } from "react";
import type { FormEvent } from "react";

import { searchPatients } from "../services/patientService";
import type { PatientFullRecord } from "../types";
import { extractErrorMessage } from "../utils/errors";

export default function PatientSearchPage(): JSX.Element {
  const [name, setName] = useState<string>("");
  const [dob, setDob] = useState<string>("");
  const [phone, setPhone] = useState<string>("");
  const [results, setResults] = useState<PatientFullRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setError(null);

    // GET /api/v1/patients/search 422s when no param is supplied at all --
    // check client-side first so the user gets an immediate, specific
    // message instead of a round trip.
    if (!name.trim() && !dob.trim() && !phone.trim()) {
      setError("Enter at least a name, date of birth, or phone number to search.");
      return;
    }

    setIsSubmitting(true);
    try {
      const found = await searchPatients({
        name: name.trim() || undefined,
        dob: dob.trim() || undefined,
        phone: phone.trim() || undefined,
      });
      setResults(found);
    } catch (submitError) {
      setError(extractErrorMessage(submitError, "Search failed. Please try again."));
      setResults(null);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="page-container">
      <h1>Patient Search</h1>

      <form className="patient-form" onSubmit={(event) => void handleSubmit(event)}>
        {error !== null && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <label className="auth-label" htmlFor="search-name">
          Name
        </label>
        <input
          id="search-name"
          className="auth-input"
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />

        <label className="auth-label" htmlFor="search-dob">
          Date of birth
        </label>
        <input
          id="search-dob"
          className="auth-input"
          type="date"
          value={dob}
          onChange={(event) => setDob(event.target.value)}
        />

        <label className="auth-label" htmlFor="search-phone">
          Phone
        </label>
        <input
          id="search-phone"
          className="auth-input"
          type="tel"
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
        />

        <button className="auth-submit" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Searching..." : "Search"}
        </button>
      </form>

      {results !== null && (
        <section>
          {results.length === 0 ? (
            <p className="empty-state">No matching patients found.</p>
          ) : (
            <ul className="search-results">
              {results.map((patient) => (
                <li key={patient.id} className="patient-card">
                  <h3>{patient.fullName}</h3>
                  <dl>
                    <dt>MRN</dt>
                    <dd>{patient.mrn}</dd>
                    <dt>Date of birth</dt>
                    <dd>{patient.dob}</dd>
                    <dt>Sex</dt>
                    <dd>{patient.sex}</dd>
                    <dt>Phone</dt>
                    <dd>{patient.phone}</dd>
                    {/* nationalId / address / allergiesNote are `undefined`
                        (key absent from the API response) for a front_desk
                        caller -- only render them when actually present. */}
                    {patient.nationalId !== undefined && (
                      <>
                        <dt>National ID</dt>
                        <dd>{patient.nationalId ?? "—"}</dd>
                      </>
                    )}
                    {patient.address !== undefined && (
                      <>
                        <dt>Address</dt>
                        <dd>{patient.address ?? "—"}</dd>
                      </>
                    )}
                    {patient.allergiesNote !== undefined && (
                      <>
                        <dt>Allergies</dt>
                        <dd>{patient.allergiesNote ?? "None on file"}</dd>
                      </>
                    )}
                  </dl>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </main>
  );
}
