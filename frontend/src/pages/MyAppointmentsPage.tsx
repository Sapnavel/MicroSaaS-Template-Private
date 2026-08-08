import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { cancelMyAppointment, listMyAppointments } from "../services/patientPortalService";
import type { Appointment } from "../types";
import { extractErrorMessage } from "../utils/errors";

const CANCELLABLE_STATUSES: Appointment["status"][] = ["booked", "checked_in"];

/** `/me/appointments` -- a patient's own appointments, with cancel. Booking
 * a new one is a separate page (`/me/appointments/new`, `BookMyAppointmentPage`). */
export default function MyAppointmentsPage(): JSX.Element {
  const [appointments, setAppointments] = useState<Appointment[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = (): void => {
    listMyAppointments()
      .then(setAppointments)
      .catch((error: unknown) => {
        setLoadError(extractErrorMessage(error, "Could not load your appointments."));
        setAppointments([]);
      });
  };

  useEffect(load, []);

  const handleCancel = async (appointmentId: string): Promise<void> => {
    setActionError(null);
    setCancellingId(appointmentId);
    try {
      await cancelMyAppointment(appointmentId);
      setAppointments((previous) =>
        (previous ?? []).map((a) => (a.id === appointmentId ? { ...a, status: "cancelled" } : a)),
      );
    } catch (error) {
      setActionError(extractErrorMessage(error, "Could not cancel this appointment."));
    } finally {
      setCancellingId(null);
    }
  };

  return (
    <main className="page-container">
      <div className="section-header">
        <h1>My appointments</h1>
        <Link className="button-secondary" to="/me/appointments/new">
          Book an appointment
        </Link>
      </div>
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
      {appointments === null ? (
        <p className="empty-state">Loading...</p>
      ) : appointments.length === 0 ? (
        <p className="empty-state">You have no appointments yet.</p>
      ) : (
        <ul className="list-plain">
          {appointments.map((appointment) => (
            <li key={appointment.id} className="list-item-card">
              <dl>
                <dt>Status</dt>
                <dd>{appointment.status}</dd>
                <dt>Doctor</dt>
                <dd>{appointment.doctorId}</dd>
                {appointment.isEmergency && <dd className="badge badge-primary">Emergency</dd>}
              </dl>
              {CANCELLABLE_STATUSES.includes(appointment.status) && (
                <button
                  className="button-secondary"
                  type="button"
                  disabled={cancellingId === appointment.id}
                  onClick={() => void handleCancel(appointment.id)}
                >
                  {cancellingId === appointment.id ? "Cancelling..." : "Cancel"}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
