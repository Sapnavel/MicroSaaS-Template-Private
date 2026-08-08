import { useEffect, useState } from "react";

import { getMyTimeline } from "../services/patientPortalService";
import type { TimelineEvent } from "../types";
import { extractErrorMessage } from "../utils/errors";

const EVENT_ICON: Record<string, string> = {
  appointment: "📅",
  consultation: "🩺",
  lab_order: "🧪",
  prescription: "💊",
  admission: "🛏️",
  invoice: "🧾",
};

const EVENT_LABEL: Record<string, string> = {
  appointment: "Appointment",
  consultation: "Consultation",
  lab_order: "Lab order",
  prescription: "Prescription",
  admission: "Ward admission",
  invoice: "Invoice",
};

/**
 * `/me/timeline` -- HMS Project Completion Prompt gap ("Patient medical
 * timeline"): before this, a patient's history was only reachable
 * piecemeal, one module's own list page at a time (My appointments, My
 * consultations, My lab reports, My bills, each a separate page). This page
 * is a single, merged, most-recent-first view across all of them, backed by
 * `GET /api/v1/me/timeline` (`patient_timeline_service.get_patient_timeline`
 * on the backend).
 *
 * Judgment call: this is a read-only overview, not a replacement for the
 * dedicated per-module pages -- clicking through to full detail (e.g. a lab
 * result's full report, an invoice's line items) still means visiting "My
 * lab reports"/"My bills" etc.; this page doesn't duplicate their detail
 * views, it's the "what happened, in order" summary those pages don't
 * provide on their own.
 */
export default function MyTimelinePage(): JSX.Element {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    getMyTimeline()
      .then(setEvents)
      .catch((error: unknown) => {
        setLoadError(extractErrorMessage(error, "Could not load your timeline."));
        setEvents([]);
      });
  }, []);

  return (
    <main className="page-container">
      <h1>My timeline</h1>
      <p className="field-hint">A combined, most-recent-first view of your care history.</p>

      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}

      {events === null ? (
        <p className="empty-state">Loading...</p>
      ) : events.length === 0 ? (
        <p className="empty-state">No history on file yet.</p>
      ) : (
        <ul className="list-plain">
          {events.map((event) => (
            <li key={`${event.eventType}-${event.id}`} className="list-item-card">
              <div className="bed-card-header">
                <h3>
                  {EVENT_ICON[event.eventType] ?? "•"} {EVENT_LABEL[event.eventType] ?? event.eventType}
                </h3>
                <span className="field-hint">{new Date(event.occurredAt).toLocaleString()}</span>
              </div>
              <p>{event.summary}</p>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
