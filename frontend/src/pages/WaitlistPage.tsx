import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import DoctorSelect from "../components/selects/DoctorSelect";
import PatientSearchSelect from "../components/selects/PatientSearchSelect";
import { useAuth } from "../hooks/useAuth";
import {
  cancelWaitlistEntry,
  fulfillWaitlistEntry,
  joinWaitlist,
  listWaitlist,
  type WaitlistEntry,
} from "../services/waitlistService";
import { extractErrorMessage } from "../utils/errors";

/** `/waitlist` -- staff view of the appointment waiting list (HMS Project
 * Completion Prompt section 3.3, "Waiting list" / "Fair slot allocation").
 * Distinct from the Queue Board (`/queue/board`): this is patients waiting
 * for a FUTURE slot with a specific doctor to open up, not patients
 * physically checked in today. Entries are listed oldest-first -- the same
 * FIFO order the backend's fairness algorithm allocates freed slots in
 * (see services/waitlist_service.py). */
export default function WaitlistPage(): JSX.Element {
  const { user } = useAuth();
  const [branchId, setBranchId] = useState<string>(user?.branchId ?? "");
  const [doctorId, setDoctorId] = useState<string>("");

  const [entries, setEntries] = useState<WaitlistEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [joinPatientId, setJoinPatientId] = useState<string>("");
  const [joinDoctorId, setJoinDoctorId] = useState<string>("");
  const [requestedDate, setRequestedDate] = useState<string>("");
  const [joinError, setJoinError] = useState<string | null>(null);
  const [isJoining, setIsJoining] = useState<boolean>(false);

  const [fulfillingId, setFulfillingId] = useState<string | null>(null);
  const [appointmentIdByEntry, setAppointmentIdByEntry] = useState<Record<string, string>>({});
  const [actionError, setActionError] = useState<string | null>(null);

  const load = (): void => {
    listWaitlist(doctorId || undefined)
      .then(setEntries)
      .catch((error: unknown) => {
        setLoadError(extractErrorMessage(error, "Could not load the waiting list."));
        setEntries([]);
      });
  };

  useEffect(load, [doctorId]);

  const handleJoin = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!joinPatientId || !joinDoctorId || !requestedDate) return;
    setJoinError(null);
    setIsJoining(true);
    try {
      await joinWaitlist({ patientId: joinPatientId, doctorId: joinDoctorId, requestedDate });
      setJoinPatientId("");
      setRequestedDate("");
      load();
    } catch (error) {
      setJoinError(extractErrorMessage(error, "Could not add this patient to the waiting list."));
    } finally {
      setIsJoining(false);
    }
  };

  const handleFulfill = async (entryId: string): Promise<void> => {
    const appointmentId = appointmentIdByEntry[entryId];
    if (!appointmentId) return;
    setActionError(null);
    setFulfillingId(entryId);
    try {
      const updated = await fulfillWaitlistEntry(entryId, appointmentId);
      setEntries((previous) => (previous ?? []).map((e) => (e.id === entryId ? updated : e)));
    } catch (error) {
      setActionError(extractErrorMessage(error, "Could not fulfill this waitlist entry."));
    } finally {
      setFulfillingId(null);
    }
  };

  const handleCancel = async (entryId: string): Promise<void> => {
    setActionError(null);
    try {
      await cancelWaitlistEntry(entryId);
      setEntries((previous) => (previous ?? []).map((e) => (e.id === entryId ? { ...e, status: "cancelled" } : e)));
    } catch (error) {
      setActionError(extractErrorMessage(error, "Could not cancel this waitlist entry."));
    }
  };

  return (
    <main className="page-container">
      <h1>Waiting list</h1>

      <form className="patient-form" onSubmit={(event) => void handleJoin(event)}>
        <h3>Add to waiting list</h3>
        {joinError !== null && (
          <p className="auth-error" role="alert">
            {joinError}
          </p>
        )}
        <label className="auth-label" htmlFor="waitlist-branch">
          Branch
        </label>
        <BranchSelect id="waitlist-branch" value={branchId} onChange={setBranchId} required />

        <label className="auth-label" htmlFor="waitlist-patient">
          Patient
        </label>
        <PatientSearchSelect id="waitlist-patient" value={joinPatientId} onChange={setJoinPatientId} />

        <label className="auth-label" htmlFor="waitlist-join-doctor">
          Doctor
        </label>
        <DoctorSelect id="waitlist-join-doctor" value={joinDoctorId} onChange={setJoinDoctorId} branchId={branchId} />

        <label className="auth-label" htmlFor="waitlist-date">
          Requested date
        </label>
        <input
          id="waitlist-date"
          className="auth-input"
          type="date"
          value={requestedDate}
          onChange={(event) => setRequestedDate(event.target.value)}
          required
        />

        <button className="auth-submit" type="submit" disabled={isJoining}>
          {isJoining ? "Adding..." : "Add to waiting list"}
        </button>
      </form>

      <div className="section-header">
        <h2>Current waiting list</h2>
      </div>
      <label className="auth-label" htmlFor="waitlist-filter-doctor">
        Filter by doctor (optional)
      </label>
      <DoctorSelect id="waitlist-filter-doctor" value={doctorId} onChange={setDoctorId} branchId={branchId} />

      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}
      {actionError !== null && (
        <p className="auth-error" role="alert">
          {actionError}
        </p>
      )}
      {entries === null ? (
        <p className="empty-state">Loading...</p>
      ) : entries.length === 0 ? (
        <p className="empty-state">Nobody is on the waiting list.</p>
      ) : (
        <ul className="list-plain">
          {entries.map((entry) => (
            <li key={entry.id} className="list-item-card">
              <dl>
                <dt>Patient</dt>
                <dd>{entry.patientId}</dd>
                <dt>Doctor</dt>
                <dd>{entry.doctorId}</dd>
                <dt>Requested date</dt>
                <dd>{entry.requestedDate}</dd>
                <dt>Status</dt>
                <dd>{entry.status}</dd>
              </dl>
              {(entry.status === "waiting" || entry.status === "offered") && (
                <>
                  <input
                    className="auth-input"
                    type="text"
                    placeholder="Appointment ID once booked"
                    value={appointmentIdByEntry[entry.id] ?? ""}
                    onChange={(event) =>
                      setAppointmentIdByEntry((previous) => ({ ...previous, [entry.id]: event.target.value }))
                    }
                  />
                  <button
                    className="button-secondary"
                    type="button"
                    disabled={fulfillingId === entry.id}
                    onClick={() => void handleFulfill(entry.id)}
                  >
                    {fulfillingId === entry.id ? "Fulfilling..." : "Mark fulfilled"}
                  </button>
                  <button className="button-secondary" type="button" onClick={() => void handleCancel(entry.id)}>
                    Remove from list
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
