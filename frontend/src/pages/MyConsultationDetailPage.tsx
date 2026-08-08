import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { getMyConsultation } from "../services/patientPortalService";
import type { Consultation } from "../types";
import { extractErrorMessage } from "../utils/errors";

/** `/me/consultations/:id` -- read-only: symptoms, diagnoses, and
 * prescriptions for one of the patient's own past consultations. Unlike
 * `ConsultationPage.tsx` (the doctor/nurse-facing equivalent), this page
 * has no write actions at all -- a patient never edits their own clinical
 * record. */
export default function MyConsultationDetailPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const [consultation, setConsultation] = useState<Consultation | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getMyConsultation(id)
      .then(setConsultation)
      .catch((error: unknown) => setLoadError(extractErrorMessage(error, "Could not load this consultation.")));
  }, [id]);

  if (loadError !== null) {
    return (
      <main className="page-container">
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      </main>
    );
  }

  if (!consultation) {
    return (
      <main className="page-container">
        <p className="empty-state">Loading...</p>
      </main>
    );
  }

  return (
    <main className="page-container">
      <h1>Consultation</h1>
      <div className="list-item-card">
        <dl>
          <dt>Date</dt>
          <dd>{new Date(consultation.startedAt).toLocaleString()}</dd>
          <dt>Symptoms</dt>
          <dd>{consultation.symptoms}</dd>
          <dt>Notes</dt>
          <dd>{consultation.notes ?? "—"}</dd>
          <dt>Status</dt>
          <dd>{consultation.endedAt !== null ? "Completed" : "In progress"}</dd>
        </dl>
      </div>

      <div className="section-header">
        <h2>Diagnoses</h2>
      </div>
      {consultation.diagnoses.length === 0 ? (
        <p className="empty-state">No diagnoses recorded.</p>
      ) : (
        <ul className="list-plain">
          {consultation.diagnoses.map((diagnosis) => (
            <li key={diagnosis.id} className="list-item-card">
              <strong>{diagnosis.icdCode}</strong> — {diagnosis.description}
              {diagnosis.isPrimary && <span className="badge badge-primary">Primary</span>}
            </li>
          ))}
        </ul>
      )}

      <div className="section-header">
        <h2>Prescriptions</h2>
      </div>
      {consultation.prescriptions.length === 0 ? (
        <p className="empty-state">No prescriptions from this visit.</p>
      ) : (
        <ul className="list-plain">
          {consultation.prescriptions.map((prescription) => (
            <li key={prescription.id} className="list-item-card">
              <p>Status: {prescription.status}</p>
              <ul className="list-plain">
                {prescription.items.map((item) => (
                  <li key={item.id}>
                    {item.dosage}, {item.frequency}
                    {item.durationDays !== null && ` for ${item.durationDays} day(s)`}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
