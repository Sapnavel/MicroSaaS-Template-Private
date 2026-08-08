import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listMyConsultations } from "../services/patientPortalService";
import type { ConsultationListItem } from "../types";
import { extractErrorMessage } from "../utils/errors";

/** `/me/consultations` -- a patient's own consultation history. */
export default function MyConsultationsPage(): JSX.Element {
  const [consultations, setConsultations] = useState<ConsultationListItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    listMyConsultations()
      .then(setConsultations)
      .catch((error: unknown) => {
        setLoadError(extractErrorMessage(error, "Could not load your consultation history."));
        setConsultations([]);
      });
  }, []);

  return (
    <main className="page-container">
      <h1>My consultations</h1>
      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}
      {consultations === null ? (
        <p className="empty-state">Loading...</p>
      ) : consultations.length === 0 ? (
        <p className="empty-state">No consultations on file yet.</p>
      ) : (
        <ul className="list-plain">
          {consultations.map((consultation) => (
            <li key={consultation.id} className="list-item-card">
              <Link to={`/me/consultations/${consultation.id}`}>
                {new Date(consultation.createdAt).toLocaleString()} — Dr. {consultation.doctorName}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
