import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { createMyPatient, getMyPatient, type CreateMyPatientData } from "../services/patientPortalService";
import type { PatientFullRecord } from "../types";
import { extractErrorMessage } from "../utils/errors";

const emptyForm: CreateMyPatientData = {
  fullName: "",
  dob: "",
  sex: "",
  phone: "",
  nationalId: "",
  address: "",
  allergiesNote: "",
};

/**
 * `/me/profile` -- a patient's own clinical record (`Patient.user_id`
 * linked to their portal login, see services/patient_portal_service.py).
 * Shows the record read-only once it exists; otherwise shows a one-time
 * "complete your profile" form. There is no edit path here (the backend
 * exposes no PATCH for a patient's own record yet) -- once created, this
 * page is read-only.
 */
export default function MyProfilePage(): JSX.Element {
  const [patient, setPatient] = useState<PatientFullRecord | null | undefined>(undefined);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [form, setForm] = useState<CreateMyPatientData>(emptyForm);
  const [createError, setCreateError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    getMyPatient()
      .then((found) => {
        if (!cancelled) setPatient(found);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load your profile."));
          setPatient(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleCreate = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setCreateError(null);
    setIsCreating(true);
    try {
      const created = await createMyPatient(form);
      setPatient(created);
    } catch (error) {
      setCreateError(extractErrorMessage(error, "Could not save your profile."));
    } finally {
      setIsCreating(false);
    }
  };

  if (patient === undefined) {
    return (
      <main className="page-container">
        <p className="empty-state">Loading...</p>
      </main>
    );
  }

  if (patient !== null) {
    return (
      <main className="page-container">
        <h1>My profile</h1>
        <div className="list-item-card">
          <dl>
            <dt>MRN</dt>
            <dd>{patient.mrn}</dd>
            <dt>Full name</dt>
            <dd>{patient.fullName}</dd>
            <dt>Date of birth</dt>
            <dd>{patient.dob}</dd>
            <dt>Sex</dt>
            <dd>{patient.sex}</dd>
            <dt>Phone</dt>
            <dd>{patient.phone}</dd>
            <dt>Address</dt>
            <dd>{patient.address ?? "—"}</dd>
            <dt>Known allergies (self-reported)</dt>
            <dd>{patient.allergiesNote ?? "None recorded"}</dd>
          </dl>
        </div>
      </main>
    );
  }

  return (
    <main className="page-container">
      <h1>Complete your profile</h1>
      <p>We need a few details before you can book appointments or view your records.</p>
      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}
      <form className="patient-form" onSubmit={(event) => void handleCreate(event)}>
        {createError !== null && (
          <p className="auth-error" role="alert">
            {createError}
          </p>
        )}
        <label className="auth-label" htmlFor="profile-full-name">
          Full name
        </label>
        <input
          id="profile-full-name"
          className="auth-input"
          type="text"
          value={form.fullName}
          onChange={(event) => setForm((previous) => ({ ...previous, fullName: event.target.value }))}
          required
        />
        <label className="auth-label" htmlFor="profile-dob">
          Date of birth
        </label>
        <input
          id="profile-dob"
          className="auth-input"
          type="date"
          value={form.dob}
          onChange={(event) => setForm((previous) => ({ ...previous, dob: event.target.value }))}
          required
        />
        <label className="auth-label" htmlFor="profile-sex">
          Sex
        </label>
        <input
          id="profile-sex"
          className="auth-input"
          type="text"
          placeholder="e.g. F, M"
          value={form.sex}
          onChange={(event) => setForm((previous) => ({ ...previous, sex: event.target.value }))}
          required
        />
        <label className="auth-label" htmlFor="profile-phone">
          Phone
        </label>
        <input
          id="profile-phone"
          className="auth-input"
          type="tel"
          value={form.phone}
          onChange={(event) => setForm((previous) => ({ ...previous, phone: event.target.value }))}
          required
        />
        <label className="auth-label" htmlFor="profile-address">
          Address (optional)
        </label>
        <input
          id="profile-address"
          className="auth-input"
          type="text"
          value={form.address}
          onChange={(event) => setForm((previous) => ({ ...previous, address: event.target.value }))}
        />
        <label className="auth-label" htmlFor="profile-allergies">
          Known allergies (optional)
        </label>
        <input
          id="profile-allergies"
          className="auth-input"
          type="text"
          placeholder="e.g. Penicillin"
          value={form.allergiesNote}
          onChange={(event) => setForm((previous) => ({ ...previous, allergiesNote: event.target.value }))}
        />
        <button className="auth-submit" type="submit" disabled={isCreating}>
          {isCreating ? "Saving..." : "Save profile"}
        </button>
      </form>
    </main>
  );
}
