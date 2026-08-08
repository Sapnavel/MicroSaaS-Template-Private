import { useState } from "react";
import type { FormEvent } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import DoctorSelect from "../components/selects/DoctorSelect";
import PatientSearchSelect from "../components/selects/PatientSearchSelect";
import RoomSelect from "../components/selects/RoomSelect";
import { useAuth } from "../hooks/useAuth";
import {
  listOTSchedules,
  OTConflictError,
  scheduleOT,
  type OTConflictReason,
} from "../services/wardService";
import type { OTSchedule } from "../types";
import { extractErrorMessage } from "../utils/errors";

interface ScheduleFormState {
  roomId: string;
  patientId: string;
  surgeonId: string;
  startTime: string;
  durationMinutes: string;
}

const emptyScheduleForm: ScheduleFormState = {
  roomId: "",
  patientId: "",
  surgeonId: "",
  startTime: "",
  durationMinutes: "",
};

interface ListFilterState {
  roomId: string;
  surgeonId: string;
  fromTime: string;
  toTime: string;
}

const emptyListFilters: ListFilterState = { roomId: "", surgeonId: "", fromTime: "", toTime: "" };

/**
 * The three distinct 409 `reason` values from `POST /wards/ot-schedule` --
 * see PRPs/ward-bed-ot-module-prp.md design decision #3. Deliberately kept
 * as three different sentences, not collapsed into one generic "slot
 * unavailable" message, since the remediation differs for each: pick a
 * different time (surgeon conflicts) vs. pick a different room (room
 * conflict).
 */
function conflictMessage(reason: OTConflictReason): string {
  switch (reason) {
    case "surgeon_busy_ot":
      return "This surgeon is already booked for another OT during this time.";
    case "surgeon_busy_appointment":
      return "This surgeon has a conflicting clinic appointment during this time.";
    case "room_conflict":
      return "This room is already booked during this time.";
    default:
      return "This OT slot is unavailable.";
  }
}

/**
 * OT scheduling -- see PRPs/ward-bed-ot-module-prp.md "ENDPOINTS" /
 * "FILES TO CREATE". `room_id`/`surgeon_id` are now `<RoomSelect>`/
 * `<DoctorSelect>` dropdowns and `patient_id` a `<PatientSearchSelect>`
 * (frontend UUID-to-dropdown conversion follow-up) -- both `RoomSelect` and
 * `DoctorSelect` require a `branch_id` to query, so this page now also
 * carries a `branchId` scope, mirroring the "implicit branch for
 * non-admin, explicit `<BranchSelect>` picker for `system_admin`" pattern
 * `BedMatrixPage.tsx` established, shared by both the create form and the
 * existing-schedule filters (one branch scope for the whole page, not two
 * independent ones). The existing-schedule view is a plain list ordered by
 * `startTime` (client-side sorted, since the contract doesn't guarantee
 * server ordering) rather than an actual calendar widget -- the PRP
 * explicitly says a calendar isn't required.
 *
 * Role gating: the create form is only rendered for `doctor`/
 * `system_admin` (a `nurse` reaching this page via
 * `ProtectedRoute`'s `allowedRoles` sees the list only, per the PRP's
 * endpoint auth column: `POST` is doctor/system_admin, `GET` is doctor/
 * nurse/system_admin).
 *
 * On a 409 (`OTConflictError`), the message is chosen from the backend's
 * `reason` field so "surgeon busy in another OT" / "surgeon has a clinic
 * appointment" / "room already booked" read as three distinct, actionable
 * messages -- see `conflictMessage` above -- rather than one generic
 * "slot unavailable".
 */
export default function OTSchedulePage(): JSX.Element {
  const { user } = useAuth();
  const isSystemAdmin = user?.role === "system_admin";
  const canSchedule = user?.role === "doctor" || isSystemAdmin;

  const [branchId, setBranchId] = useState<string>("");
  const effectiveBranchId = isSystemAdmin ? branchId : user?.branchId ?? "";

  const [form, setForm] = useState<ScheduleFormState>(emptyScheduleForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<OTConflictError | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [scheduled, setScheduled] = useState<OTSchedule | null>(null);

  const [filters, setFilters] = useState<ListFilterState>(emptyListFilters);
  const [schedules, setSchedules] = useState<OTSchedule[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [isLoadingList, setIsLoadingList] = useState<boolean>(false);

  const loadSchedules = async (): Promise<void> => {
    setIsLoadingList(true);
    setListError(null);
    try {
      const found = await listOTSchedules({
        roomId: filters.roomId.trim() || undefined,
        surgeonId: filters.surgeonId.trim() || undefined,
        fromTime: filters.fromTime ? new Date(filters.fromTime).toISOString() : undefined,
        toTime: filters.toTime ? new Date(filters.toTime).toISOString() : undefined,
      });
      const sorted = [...found].sort(
        (a, b) => new Date(a.startTime).getTime() - new Date(b.startTime).getTime(),
      );
      setSchedules(sorted);
    } catch (error) {
      setListError(extractErrorMessage(error, "Could not load the OT schedule."));
    } finally {
      setIsLoadingList(false);
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setFormError(null);
    setConflict(null);
    setScheduled(null);

    if (!form.roomId.trim() || !form.patientId.trim() || !form.surgeonId.trim() || !form.startTime) {
      setFormError("Room ID, patient ID, surgeon ID, and start time are all required.");
      return;
    }
    const durationMinutes = Number(form.durationMinutes);
    if (!Number.isInteger(durationMinutes) || durationMinutes <= 0) {
      setFormError("Duration must be a positive whole number of minutes.");
      return;
    }

    setIsSubmitting(true);
    try {
      const created = await scheduleOT({
        roomId: form.roomId.trim(),
        patientId: form.patientId.trim(),
        surgeonId: form.surgeonId.trim(),
        startTime: new Date(form.startTime).toISOString(),
        durationMinutes,
      });
      setScheduled(created);
      setForm(emptyScheduleForm);
      // The new booking may match the current list filters -- refresh so
      // it shows up immediately rather than requiring a manual reload.
      void loadSchedules();
    } catch (error) {
      if (error instanceof OTConflictError) {
        setConflict(error);
      } else {
        setFormError(extractErrorMessage(error, "Could not schedule this OT slot."));
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="page-container">
      <h1>OT schedule</h1>

      {isSystemAdmin && (
        <>
          <div className="section-header">
            <h2>Branch</h2>
          </div>
          <div className="patient-form">
            <label className="auth-label" htmlFor="ot-branch-id">
              Branch
            </label>
            <BranchSelect id="ot-branch-id" value={branchId} onChange={setBranchId} />
          </div>
        </>
      )}

      {canSchedule && (
        <>
          <div className="section-header">
            <h2>Schedule a slot</h2>
          </div>
          <form className="patient-form" onSubmit={(event) => void handleSubmit(event)}>
            {formError !== null && (
              <p className="auth-error" role="alert">
                {formError}
              </p>
            )}

            {conflict !== null && (
              <div className="status-banner status-banner--override" role="alert">
                <h3>Slot unavailable</h3>
                <p>{conflictMessage(conflict.reason)}</p>
              </div>
            )}

            {scheduled !== null && (
              <div className="status-banner status-banner--success">
                <p>
                  Scheduled room {scheduled.roomId} from {scheduled.startTime} to {scheduled.endTime}.
                </p>
              </div>
            )}

            <label className="auth-label" htmlFor="ot-room-id">
              Room
            </label>
            <RoomSelect
              id="ot-room-id"
              value={form.roomId}
              onChange={(value) => setForm((previous) => ({ ...previous, roomId: value }))}
              branchId={effectiveBranchId}
              required
            />

            <label className="auth-label" htmlFor="ot-patient-id">
              Patient
            </label>
            <PatientSearchSelect
              id="ot-patient-id"
              value={form.patientId}
              onChange={(value) => setForm((previous) => ({ ...previous, patientId: value }))}
            />

            <label className="auth-label" htmlFor="ot-surgeon-id">
              Surgeon
            </label>
            <DoctorSelect
              id="ot-surgeon-id"
              value={form.surgeonId}
              onChange={(value) => setForm((previous) => ({ ...previous, surgeonId: value }))}
              branchId={effectiveBranchId}
              required
            />

            <label className="auth-label" htmlFor="ot-start-time">
              Start time
            </label>
            <input
              id="ot-start-time"
              className="auth-input"
              type="datetime-local"
              value={form.startTime}
              onChange={(event) => setForm((previous) => ({ ...previous, startTime: event.target.value }))}
              required
            />

            <label className="auth-label" htmlFor="ot-duration">
              Duration (minutes)
            </label>
            <input
              id="ot-duration"
              className="auth-input"
              type="number"
              min={1}
              value={form.durationMinutes}
              onChange={(event) =>
                setForm((previous) => ({ ...previous, durationMinutes: event.target.value }))
              }
              required
            />

            <button className="auth-submit" type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Scheduling..." : "Schedule"}
            </button>
          </form>
        </>
      )}

      <div className="section-header">
        <h2>Existing schedule</h2>
      </div>
      <div className="patient-form">
        <label className="auth-label" htmlFor="ot-filter-room">
          Room (optional)
        </label>
        <RoomSelect
          id="ot-filter-room"
          value={filters.roomId}
          onChange={(value) => setFilters((previous) => ({ ...previous, roomId: value }))}
          branchId={effectiveBranchId}
        />
        <label className="auth-label" htmlFor="ot-filter-surgeon">
          Surgeon (optional)
        </label>
        <DoctorSelect
          id="ot-filter-surgeon"
          value={filters.surgeonId}
          onChange={(value) => setFilters((previous) => ({ ...previous, surgeonId: value }))}
          branchId={effectiveBranchId}
        />
        <label className="auth-label" htmlFor="ot-filter-from">
          From (optional)
        </label>
        <input
          id="ot-filter-from"
          className="auth-input"
          type="datetime-local"
          value={filters.fromTime}
          onChange={(event) => setFilters((previous) => ({ ...previous, fromTime: event.target.value }))}
        />
        <label className="auth-label" htmlFor="ot-filter-to">
          To (optional)
        </label>
        <input
          id="ot-filter-to"
          className="auth-input"
          type="datetime-local"
          value={filters.toTime}
          onChange={(event) => setFilters((previous) => ({ ...previous, toTime: event.target.value }))}
        />
        <button className="button-secondary" type="button" disabled={isLoadingList} onClick={() => void loadSchedules()}>
          {isLoadingList ? "Loading..." : "Load schedule"}
        </button>
      </div>

      {listError !== null && (
        <p className="auth-error" role="alert">
          {listError}
        </p>
      )}

      {schedules === null ? (
        <p className="empty-state">Enter filters above (or leave them blank) and click "Load schedule".</p>
      ) : schedules.length === 0 ? (
        <p className="empty-state">No OT slots match the current filters.</p>
      ) : (
        <ul className="list-plain">
          {schedules.map((entry) => (
            <li key={entry.id} className="list-item-card">
              <strong>{entry.startTime}</strong> — {entry.endTime}
              <p className="field-hint">
                Room {entry.roomId} · Surgeon {entry.surgeonId} · Patient {entry.patientId}
              </p>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
