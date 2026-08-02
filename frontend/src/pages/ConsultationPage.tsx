import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { useAuth } from "../hooks/useAuth";
import {
  addAllergy,
  addDiagnosis,
  completeConsultation,
  getConsultation,
  listAllergies,
  type AddAllergyData,
  type AddDiagnosisData,
} from "../services/consultationService";
import type { Consultation, PatientAllergy } from "../types";
import { extractErrorMessage } from "../utils/errors";

const emptyDiagnosisForm: AddDiagnosisData = {
  icdCode: "",
  description: "",
  isPrimary: false,
};

const emptyAllergyForm: AddAllergyData = {
  substance: "",
  severity: "mild",
  reaction: "",
};

/**
 * Consultation detail page: symptoms/notes, diagnoses, patient allergies,
 * and a "complete consultation" action -- see
 * PRPs/clinical-consultation-prescription-prp.md "ENDPOINTS".
 *
 * Judgment call: the contract has no standalone "update notes" endpoint --
 * only `PATCH /consultations/{id}/complete` accepts `notes`, which also
 * sets `ended_at`. So editing notes and completing the consultation are
 * necessarily the same submit action here, not two separate ones. Once a
 * consultation is completed, notes/diagnoses become read-only in this UI
 * (the contract doesn't say whether the API would even accept further
 * writes post-completion, and offering an action that might 4xx isn't
 * better than just not offering it).
 */
export default function ConsultationPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const { user } = useAuth();

  // Adding a diagnosis and completing a consultation are doctor(own)/
  // system_admin only per the ENDPOINTS table -- a nurse calling either
  // would get a 403, so the UI hides those actions rather than offering
  // one that always fails. Allergy recording stays available to nurses --
  // the PRP calls this out as "the one clinical-write nurses get in this
  // module."
  const canWriteConsultation = user?.role !== "nurse";

  const [consultation, setConsultation] = useState<Consultation | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [allergies, setAllergies] = useState<PatientAllergy[] | null>(null);
  const [allergyLoadError, setAllergyLoadError] = useState<string | null>(null);

  const [diagnosisForm, setDiagnosisForm] = useState<AddDiagnosisData>(emptyDiagnosisForm);
  const [diagnosisError, setDiagnosisError] = useState<string | null>(null);
  const [isAddingDiagnosis, setIsAddingDiagnosis] = useState<boolean>(false);

  const [allergyForm, setAllergyForm] = useState<AddAllergyData>(emptyAllergyForm);
  const [addAllergyError, setAddAllergyError] = useState<string | null>(null);
  const [isAddingAllergy, setIsAddingAllergy] = useState<boolean>(false);

  const [notes, setNotes] = useState<string>("");
  const [completeError, setCompleteError] = useState<string | null>(null);
  const [isCompleting, setIsCompleting] = useState<boolean>(false);

  useEffect(() => {
    if (!id) {
      return;
    }
    const load = async (): Promise<void> => {
      try {
        const found = await getConsultation(id);
        setConsultation(found);
        setNotes(found.notes ?? "");
      } catch (error) {
        setLoadError(extractErrorMessage(error, "Could not load this consultation."));
      }
    };
    void load();
  }, [id]);

  useEffect(() => {
    if (!consultation) {
      return;
    }
    const loadAllergies = async (): Promise<void> => {
      try {
        const found = await listAllergies(consultation.patientId);
        setAllergies(found);
      } catch (error) {
        setAllergyLoadError(extractErrorMessage(error, "Could not load this patient's allergies."));
      }
    };
    void loadAllergies();
  }, [consultation]);

  const handleAddDiagnosis = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!consultation) {
      return;
    }
    setDiagnosisError(null);
    setIsAddingDiagnosis(true);
    try {
      const created = await addDiagnosis(consultation.id, diagnosisForm);
      setConsultation({ ...consultation, diagnoses: [...consultation.diagnoses, created] });
      setDiagnosisForm(emptyDiagnosisForm);
    } catch (error) {
      setDiagnosisError(extractErrorMessage(error, "Could not add this diagnosis."));
    } finally {
      setIsAddingDiagnosis(false);
    }
  };

  const handleAddAllergy = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!consultation) {
      return;
    }
    setAddAllergyError(null);
    setIsAddingAllergy(true);
    try {
      const created = await addAllergy(consultation.patientId, allergyForm);
      setAllergies((previous) => [...(previous ?? []), created]);
      setAllergyForm(emptyAllergyForm);
    } catch (error) {
      setAddAllergyError(extractErrorMessage(error, "Could not record this allergy."));
    } finally {
      setIsAddingAllergy(false);
    }
  };

  const handleComplete = async (): Promise<void> => {
    if (!consultation) {
      return;
    }
    setCompleteError(null);
    setIsCompleting(true);
    try {
      const updated = await completeConsultation(consultation.id, notes);
      setConsultation(updated);
    } catch (error) {
      setCompleteError(extractErrorMessage(error, "Could not complete this consultation."));
    } finally {
      setIsCompleting(false);
    }
  };

  if (!id) {
    return (
      <main className="page-container">
        <p className="auth-error" role="alert">
          No consultation ID in the URL.
        </p>
      </main>
    );
  }

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

  const isCompleted = consultation.endedAt !== null;

  return (
    <main className="page-container">
      <h1>Consultation</h1>

      <div className="list-item-card">
        <dl>
          <dt>Patient ID</dt>
          <dd>{consultation.patientId}</dd>
          <dt>Symptoms</dt>
          <dd>{consultation.symptoms}</dd>
          <dt>Status</dt>
          <dd>{isCompleted ? `Completed at ${consultation.endedAt ?? ""}` : "In progress"}</dd>
        </dl>
      </div>

      <div className="section-header">
        <h2>Notes</h2>
      </div>
      {completeError !== null && (
        <p className="auth-error" role="alert">
          {completeError}
        </p>
      )}
      {canWriteConsultation && !isCompleted ? (
        <>
          <label className="auth-label" htmlFor="consultation-notes">
            Clinical notes
          </label>
          <textarea
            id="consultation-notes"
            className="textarea"
            rows={4}
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="Clinical notes..."
          />
          <p>
            <button
              className="auth-submit"
              type="button"
              disabled={isCompleting}
              onClick={() => void handleComplete()}
            >
              {isCompleting ? "Completing..." : "Complete consultation"}
            </button>
          </p>
        </>
      ) : (
        <p>{consultation.notes ?? "No notes recorded."}</p>
      )}

      <div className="section-header">
        <h2>Diagnoses</h2>
      </div>
      {consultation.diagnoses.length === 0 ? (
        <p className="empty-state">No diagnoses recorded yet.</p>
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

      {canWriteConsultation && !isCompleted && (
        <form className="patient-form" onSubmit={(event) => void handleAddDiagnosis(event)}>
          <h3>Add diagnosis</h3>
          {diagnosisError !== null && (
            <p className="auth-error" role="alert">
              {diagnosisError}
            </p>
          )}
          <label className="auth-label" htmlFor="diagnosis-icd-code">
            ICD code
          </label>
          <input
            id="diagnosis-icd-code"
            className="auth-input"
            type="text"
            placeholder="e.g. J20.9"
            value={diagnosisForm.icdCode}
            onChange={(event) =>
              setDiagnosisForm((previous) => ({ ...previous, icdCode: event.target.value }))
            }
            required
          />
          <label className="auth-label" htmlFor="diagnosis-description">
            Description
          </label>
          <input
            id="diagnosis-description"
            className="auth-input"
            type="text"
            value={diagnosisForm.description}
            onChange={(event) =>
              setDiagnosisForm((previous) => ({ ...previous, description: event.target.value }))
            }
            required
          />
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={diagnosisForm.isPrimary}
              onChange={(event) =>
                setDiagnosisForm((previous) => ({ ...previous, isPrimary: event.target.checked }))
              }
            />
            Primary diagnosis
          </label>
          <button className="auth-submit" type="submit" disabled={isAddingDiagnosis}>
            {isAddingDiagnosis ? "Adding..." : "Add diagnosis"}
          </button>
        </form>
      )}

      <div className="section-header">
        <h2>Patient allergies</h2>
      </div>
      {allergyLoadError !== null && (
        <p className="auth-error" role="alert">
          {allergyLoadError}
        </p>
      )}
      {allergies === null ? (
        <p className="empty-state">Loading allergies...</p>
      ) : allergies.length === 0 ? (
        <p className="empty-state">No known allergies on file.</p>
      ) : (
        <ul className="list-plain">
          {allergies.map((allergy) => (
            <li key={allergy.id} className="list-item-card">
              <strong>{allergy.substance}</strong> ({allergy.severity}) — {allergy.reaction}
            </li>
          ))}
        </ul>
      )}

      <form className="patient-form" onSubmit={(event) => void handleAddAllergy(event)}>
        <h3>Record allergy</h3>
        {addAllergyError !== null && (
          <p className="auth-error" role="alert">
            {addAllergyError}
          </p>
        )}
        <label className="auth-label" htmlFor="allergy-substance">
          Substance
        </label>
        <input
          id="allergy-substance"
          className="auth-input"
          type="text"
          value={allergyForm.substance}
          onChange={(event) =>
            setAllergyForm((previous) => ({ ...previous, substance: event.target.value }))
          }
          required
        />
        <label className="auth-label" htmlFor="allergy-severity">
          Severity
        </label>
        <select
          id="allergy-severity"
          className="auth-input"
          value={allergyForm.severity}
          onChange={(event) =>
            setAllergyForm((previous) => ({
              ...previous,
              severity: event.target.value as AddAllergyData["severity"],
            }))
          }
        >
          <option value="mild">Mild</option>
          <option value="moderate">Moderate</option>
          <option value="severe">Severe</option>
        </select>
        <label className="auth-label" htmlFor="allergy-reaction">
          Reaction
        </label>
        <input
          id="allergy-reaction"
          className="auth-input"
          type="text"
          value={allergyForm.reaction}
          onChange={(event) =>
            setAllergyForm((previous) => ({ ...previous, reaction: event.target.value }))
          }
          required
        />
        <button className="auth-submit" type="submit" disabled={isAddingAllergy}>
          {isAddingAllergy ? "Recording..." : "Record allergy"}
        </button>
      </form>

      <div className="section-header">
        <h2>Prescriptions</h2>
        {/* !isCompleted: the backend now rejects new prescriptions on a
            completed consultation (409) -- match that here so the UI
            doesn't offer an action that will just fail, same gating already
            applied to the diagnosis form above (REVIEW-AGENT finding M1). */}
        {canWriteConsultation && !isCompleted && (
          <Link className="button-secondary" to={`/consultations/${consultation.id}/prescriptions`}>
            Manage prescriptions
          </Link>
        )}
      </div>
      {consultation.prescriptions.length === 0 ? (
        <p className="empty-state">No prescriptions yet.</p>
      ) : (
        <ul className="list-plain">
          {consultation.prescriptions.map((prescription) => (
            <li key={prescription.id} className="list-item-card">
              Status: {prescription.status} — {prescription.items.length} item(s)
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
