import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  getConsultationForAppointment,
  startConsultation,
} from "../services/consultationService";
import { extractErrorMessage } from "../utils/errors";

/**
 * `/consultations/new?appointmentId=` -- the missing click-path from the
 * Queue Board to a Consultation. Previously `consultationService.
 * startConsultation` had no caller anywhere in the frontend, so there was no
 * way to actually reach a Consultation through the UI at all (see the
 * QueueBoardPage.tsx "Consultation" link this page is opened from).
 *
 * On mount, looks up whether a consultation already exists for this
 * appointment (`Consultation.appointment_id` is UNIQUE on the backend, so
 * this is a 0-or-1 lookup) and redirects straight there if so -- a doctor
 * revisiting an in-progress token shouldn't be offered a form that would
 * just 409. Only shows the "start" form when none exists yet.
 */
export default function StartConsultationPage(): JSX.Element {
  const [searchParams] = useSearchParams();
  const appointmentId = searchParams.get("appointmentId");
  const navigate = useNavigate();

  const [isChecking, setIsChecking] = useState<boolean>(true);
  const [checkError, setCheckError] = useState<string | null>(null);

  const [symptoms, setSymptoms] = useState<string>("");
  const [isStarting, setIsStarting] = useState<boolean>(false);
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(() => {
    if (!appointmentId) {
      setIsChecking(false);
      return;
    }
    let cancelled = false;
    getConsultationForAppointment(appointmentId)
      .then((existing) => {
        if (cancelled) {
          return;
        }
        if (existing) {
          navigate(`/consultations/${existing.id}`, { replace: true });
          return;
        }
        setIsChecking(false);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setCheckError(extractErrorMessage(error, "Could not check for an existing consultation."));
          setIsChecking(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [appointmentId, navigate]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!appointmentId) {
      return;
    }
    setStartError(null);
    setIsStarting(true);
    try {
      const created = await startConsultation({ appointmentId, symptoms });
      navigate(`/consultations/${created.id}`, { replace: true });
    } catch (error) {
      setStartError(extractErrorMessage(error, "Could not start this consultation."));
    } finally {
      setIsStarting(false);
    }
  };

  if (!appointmentId) {
    return (
      <main className="page-container">
        <p className="auth-error" role="alert">
          No appointment specified -- open this page from a queued token on the Queue Board.
        </p>
      </main>
    );
  }

  if (isChecking) {
    return (
      <main className="page-container">
        <p className="empty-state">Checking for an existing consultation...</p>
      </main>
    );
  }

  return (
    <main className="page-container">
      <h1>Start consultation</h1>
      {checkError !== null && (
        <p className="auth-error" role="alert">
          {checkError}
        </p>
      )}
      <form className="patient-form" onSubmit={(event) => void handleSubmit(event)}>
        {startError !== null && (
          <p className="auth-error" role="alert">
            {startError}
          </p>
        )}
        <label className="auth-label" htmlFor="start-consultation-symptoms">
          Symptoms
        </label>
        <textarea
          id="start-consultation-symptoms"
          className="textarea"
          rows={4}
          value={symptoms}
          onChange={(event) => setSymptoms(event.target.value)}
          placeholder="Presenting symptoms..."
        />
        <button className="auth-submit" type="submit" disabled={isStarting}>
          {isStarting ? "Starting..." : "Start consultation"}
        </button>
      </form>
    </main>
  );
}
