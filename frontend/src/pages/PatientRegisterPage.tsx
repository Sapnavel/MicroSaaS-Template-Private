import { useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../hooks/useAuth";
import {
  PatientConflictError,
  createPatient,
  getPatient,
  type CreatePatientData,
} from "../services/patientService";
import type { PatientFullRecord } from "../types";
import { extractErrorMessage } from "../utils/errors";

interface ConflictState {
  existingPatientId: string;
  existingPatientMrn: string;
}

const emptyForm: CreatePatientData = {
  fullName: "",
  dob: "",
  sex: "",
  phone: "",
  nationalId: "",
  address: "",
  allergiesNote: "",
};

export default function PatientRegisterPage(): JSX.Element {
  const { user } = useAuth();
  // Clinical fields (national ID, address, allergies) are only ever
  // returned to doctor/nurse/system_admin on reads (see PatientFullRecord's
  // doc comment). The contract doesn't say whether front_desk may submit
  // them on create, but showing a front-desk staffer an allergies field
  // for data they can never read back is confusing, so this form hides
  // those inputs for front_desk too -- a UI judgment call, not something
  // the API contract states explicitly.
  const canEditClinicalFields = user?.role !== "front_desk";

  const [form, setForm] = useState<CreatePatientData>(emptyForm);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [isResolvingConflict, setIsResolvingConflict] = useState<boolean>(false);
  const [createdPatient, setCreatedPatient] = useState<PatientFullRecord | null>(null);
  const [usedExisting, setUsedExisting] = useState<boolean>(false);

  const updateField = <K extends keyof CreatePatientData>(field: K, value: CreatePatientData[K]): void => {
    setForm((previous) => ({ ...previous, [field]: value }));
  };

  const submit = async (force: boolean): Promise<void> => {
    setError(null);
    setIsSubmitting(true);
    try {
      const patient = await createPatient(form, force);
      setCreatedPatient(patient);
      setConflict(null);
      setUsedExisting(false);
    } catch (submitError) {
      if (submitError instanceof PatientConflictError) {
        setConflict({
          existingPatientId: submitError.existingPatientId,
          existingPatientMrn: submitError.existingPatientMrn,
        });
      } else {
        setError(extractErrorMessage(submitError, "Registration failed. Please try again."));
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    void submit(false);
  };

  const handleUseExisting = async (): Promise<void> => {
    if (!conflict) {
      return;
    }
    setIsResolvingConflict(true);
    setError(null);
    try {
      const patient = await getPatient(conflict.existingPatientId);
      setCreatedPatient(patient);
      setUsedExisting(true);
      setConflict(null);
    } catch (fetchError) {
      setError(extractErrorMessage(fetchError, "Could not load the existing patient record."));
    } finally {
      setIsResolvingConflict(false);
    }
  };

  const handleCreateAnyway = (): void => {
    // Explicit second action, deliberately not automatic -- a wrong merge
    // corrupts two people's medical histories together, so the force
    // retry must be a distinct user-initiated click, not a silent retry.
    void submit(true);
  };

  const handleRegisterAnother = (): void => {
    setForm(emptyForm);
    setCreatedPatient(null);
    setUsedExisting(false);
    setConflict(null);
    setError(null);
  };

  if (createdPatient) {
    return (
      <main className="page-container">
        <h1>{usedExisting ? "Existing patient" : "Patient registered"}</h1>
        <div className="patient-card">
          <h3>{createdPatient.fullName}</h3>
          <dl>
            <dt>MRN</dt>
            <dd>{createdPatient.mrn}</dd>
            <dt>Date of birth</dt>
            <dd>{createdPatient.dob}</dd>
            <dt>Sex</dt>
            <dd>{createdPatient.sex}</dd>
            <dt>Phone</dt>
            <dd>{createdPatient.phone}</dd>
          </dl>
        </div>
        <p>
          <button className="button-secondary" type="button" onClick={handleRegisterAnother}>
            Register another patient
          </button>
        </p>
      </main>
    );
  }

  return (
    <main className="page-container">
      <h1>Patient Registration</h1>

      {conflict && (
        <div className="conflict-banner" role="alert">
          <p>
            A patient matching this record may already exist: MRN <strong>{conflict.existingPatientMrn}</strong>
          </p>
          <div className="conflict-actions">
            <button
              className="button-secondary"
              type="button"
              disabled={isResolvingConflict}
              onClick={() => void handleUseExisting()}
            >
              {isResolvingConflict ? "Loading..." : "Use existing patient"}
            </button>
            <button
              className="button-danger"
              type="button"
              disabled={isSubmitting}
              onClick={handleCreateAnyway}
            >
              {isSubmitting ? "Creating..." : "Create anyway"}
            </button>
          </div>
        </div>
      )}

      <form className="patient-form" onSubmit={handleSubmit}>
        {error !== null && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <label className="auth-label" htmlFor="register-patient-full-name">
          Full name
        </label>
        <input
          id="register-patient-full-name"
          className="auth-input"
          type="text"
          autoComplete="name"
          value={form.fullName}
          onChange={(event) => updateField("fullName", event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="register-patient-dob">
          Date of birth
        </label>
        <input
          id="register-patient-dob"
          className="auth-input"
          type="date"
          value={form.dob}
          onChange={(event) => updateField("dob", event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="register-patient-sex">
          Sex
        </label>
        <input
          id="register-patient-sex"
          className="auth-input"
          type="text"
          value={form.sex}
          onChange={(event) => updateField("sex", event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="register-patient-phone">
          Phone
        </label>
        <input
          id="register-patient-phone"
          className="auth-input"
          type="tel"
          autoComplete="tel"
          value={form.phone}
          onChange={(event) => updateField("phone", event.target.value)}
          required
        />

        {canEditClinicalFields && (
          <>
            <label className="auth-label" htmlFor="register-patient-national-id">
              National ID (optional)
            </label>
            <input
              id="register-patient-national-id"
              className="auth-input"
              type="text"
              value={form.nationalId}
              onChange={(event) => updateField("nationalId", event.target.value)}
            />

            <label className="auth-label" htmlFor="register-patient-address">
              Address (optional)
            </label>
            <input
              id="register-patient-address"
              className="auth-input"
              type="text"
              value={form.address}
              onChange={(event) => updateField("address", event.target.value)}
            />

            <label className="auth-label" htmlFor="register-patient-allergies">
              Allergies note (optional)
            </label>
            <input
              id="register-patient-allergies"
              className="auth-input"
              type="text"
              value={form.allergiesNote}
              onChange={(event) => updateField("allergiesNote", event.target.value)}
            />
          </>
        )}

        <button className="auth-submit" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Registering..." : "Register patient"}
        </button>
      </form>
    </main>
  );
}
