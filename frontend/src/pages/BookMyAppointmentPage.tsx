import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import BranchSelect from "../components/selects/BranchSelect";
import SpecialtySelect from "../components/selects/SpecialtySelect";
import { bookMyAppointment, searchMyDoctors } from "../services/patientPortalService";
import type { Doctor } from "../types";
import { extractErrorMessage } from "../utils/errors";

/**
 * `/me/appointments/new`. Doctor search is unscoped-by-branch reference
 * data (`GET /api/v1/me/doctors`, see services/patient_portal_service.py's
 * `search_doctors_for_booking` docstring) -- narrowed here by
 * branch/specialty pickers for usability, not because the backend requires
 * either. Room selection does not appear anywhere in this form: the backend
 * auto-assigns the first available consultation room in the chosen branch
 * (`book_my_appointment`), since a patient has no way to know which
 * physical rooms exist.
 */
export default function BookMyAppointmentPage(): JSX.Element {
  const navigate = useNavigate();

  const [branchId, setBranchId] = useState<string>("");
  const [specialtyId, setSpecialtyId] = useState<string>("");
  const [doctors, setDoctors] = useState<Doctor[] | null>(null);
  const [doctorLoadError, setDoctorLoadError] = useState<string | null>(null);

  const [doctorId, setDoctorId] = useState<string>("");
  const [startTime, setStartTime] = useState<string>("");
  const [durationMinutes, setDurationMinutes] = useState<number>(20);

  const [bookError, setBookError] = useState<string | null>(null);
  const [isBooking, setIsBooking] = useState<boolean>(false);

  useEffect(() => {
    setDoctors(null);
    setDoctorId("");
    searchMyDoctors(branchId || undefined, specialtyId ? Number(specialtyId) : undefined)
      .then(setDoctors)
      .catch((error: unknown) => {
        setDoctorLoadError(extractErrorMessage(error, "Could not load doctors."));
        setDoctors([]);
      });
  }, [branchId, specialtyId]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!branchId || !doctorId || !startTime) {
      return;
    }
    setBookError(null);
    setIsBooking(true);
    try {
      await bookMyAppointment({
        branchId,
        doctorId,
        startTime: new Date(startTime).toISOString(),
        durationMinutes,
      });
      navigate("/me/appointments", { replace: true });
    } catch (error) {
      setBookError(extractErrorMessage(error, "Could not book this appointment."));
    } finally {
      setIsBooking(false);
    }
  };

  return (
    <main className="page-container">
      <h1>Book an appointment</h1>
      <form className="patient-form" onSubmit={(event) => void handleSubmit(event)}>
        {bookError !== null && (
          <p className="auth-error" role="alert">
            {bookError}
          </p>
        )}
        <label className="auth-label" htmlFor="book-branch">
          Branch
        </label>
        <BranchSelect id="book-branch" value={branchId} onChange={setBranchId} required />

        <label className="auth-label" htmlFor="book-specialty">
          Specialty (optional)
        </label>
        <SpecialtySelect id="book-specialty" value={specialtyId} onChange={setSpecialtyId} />

        <label className="auth-label" htmlFor="book-doctor">
          Doctor
        </label>
        {doctorLoadError !== null && (
          <p className="auth-error" role="alert">
            {doctorLoadError}
          </p>
        )}
        <select
          id="book-doctor"
          className="auth-input"
          value={doctorId}
          onChange={(event) => setDoctorId(event.target.value)}
          disabled={!branchId || doctors === null}
          required
        >
          <option value="">
            {!branchId ? "Select a branch first…" : doctors === null ? "Loading…" : "Select a doctor…"}
          </option>
          {(doctors ?? []).map((doctor) => (
            <option key={doctor.id} value={doctor.id}>
              {doctor.fullName}
            </option>
          ))}
        </select>

        <label className="auth-label" htmlFor="book-start-time">
          Preferred date &amp; time
        </label>
        <input
          id="book-start-time"
          className="auth-input"
          type="datetime-local"
          value={startTime}
          onChange={(event) => setStartTime(event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="book-duration">
          Duration (minutes)
        </label>
        <input
          id="book-duration"
          className="auth-input"
          type="number"
          min={5}
          max={240}
          value={durationMinutes}
          onChange={(event) => setDurationMinutes(Number(event.target.value))}
          required
        />

        <button className="auth-submit" type="submit" disabled={isBooking}>
          {isBooking ? "Booking..." : "Book appointment"}
        </button>
      </form>
    </main>
  );
}
