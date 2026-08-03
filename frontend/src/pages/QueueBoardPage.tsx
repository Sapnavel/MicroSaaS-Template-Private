import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../hooks/useAuth";
import {
  checkInWithAppointment,
  getQueueBoard,
  subscribeToQueueBoard,
  updateTokenStatus,
} from "../services/queueService";
import type { QueueToken, TokenStatus } from "../types";
import { extractErrorMessage } from "../utils/errors";

const STATUS_BADGE_CLASS: Record<TokenStatus, string> = {
  waiting: "badge badge-warning",
  in_consultation: "badge badge-primary",
  delayed: "badge badge-danger",
  skipped: "badge badge-danger",
  done: "badge badge-success",
};

const STATUS_LABEL: Record<TokenStatus, string> = {
  waiting: "Waiting",
  in_consultation: "In consultation",
  delayed: "Delayed",
  skipped: "Skipped",
  done: "Done",
};

/**
 * Legal status transitions -- see PRPs/realtime-queue-prp.md design decision
 * #3 / `services/queue_service.py`'s `_LEGAL_TRANSITIONS`. Mirrored here
 * (read-only, UI-side) purely so action buttons never offer a transition the
 * backend would 409 on; the backend's own state machine remains the sole
 * source of truth.
 */
const LEGAL_TRANSITIONS: Record<TokenStatus, TokenStatus[]> = {
  waiting: ["in_consultation", "delayed", "skipped"],
  delayed: ["waiting", "in_consultation", "skipped"],
  skipped: ["waiting"],
  in_consultation: ["done"],
  done: [],
};

const TRANSITION_LABEL: Record<TokenStatus, string> = {
  waiting: "Requeue",
  in_consultation: "Call in",
  delayed: "Delay",
  skipped: "Skip",
  done: "Complete",
};

/**
 * Live queue board -- see PRPs/realtime-queue-prp.md "ENDPOINTS" / "FILES TO
 * CREATE". Loads the initial snapshot via `GET /queue/board`, then subscribes
 * to the existing `GET /ws/queue/{branch_id}/{department_id}` channel (design
 * decision #4) for live patches, same "patch the local list, don't force a
 * full reload" discipline `InvoicePage.tsx`'s `applyUpdatedInvoice` uses.
 *
 * Role gating (see the PRP's design decision #6 / "ENDPOINTS" auth column):
 *  - `front_desk` / `nurse` / `system_admin`: check-in form, plus a
 *    status-transition button on every row for which a legal transition
 *    exists (`LEGAL_TRANSITIONS` above).
 *  - `doctor`: the same table, but strictly read-only -- no check-in form,
 *    no action buttons on any row -- matching "doctor may only read the live
 *    queue snapshot" from design decision #6.
 *
 * Judgment calls (documented per FRONTEND-AGENT instructions):
 *  - `branchId`/`departmentId` UX mirrors `BedMatrixPage.tsx`/
 *    `NotificationHistoryPage.tsx`: free-text branch-ID input (no
 *    branch-lookup endpoint exists anywhere in this codebase) plus a numeric
 *    department-ID input. Unlike those pages, `departmentId` is required
 *    here (not optional) for *every* role, even though `GET /queue/board`
 *    itself treats it as optional -- the WebSocket subscription needs one
 *    concrete `(branch_id, department_id)` pair to scope to, and this page
 *    always keeps the live board and the WebSocket subscription pointed at
 *    the same pair rather than offering a "load an unscoped, all-departments
 *    snapshot that then can't subscribe to a single live channel" mode.
 *  - **Check-in scope**: only appointment-linked check-in
 *    (`checkInWithAppointment`) is exposed in this page's UI. Walk-in
 *    check-in (`checkInWalkIn`, `{branch_id, department_id}`) is a real,
 *    already-implemented capability in `queueService.ts`, but is
 *    deliberately left off this page: the common case per design decision
 *    #1 is a patient arriving for a booked appointment, and a walk-in token
 *    (no `appointment_id`, so no patient linkage at all) is a schema-imposed
 *    edge case rather than the primary desk workflow. Wiring a second
 *    "walk-in" form here would be a small addition if a future PRP asks for
 *    it explicitly; this page keeps the scope to the one form the primary
 *    workflow needs.
 *  - **WebSocket lifecycle**: `subscribeToQueueBoard` is opened in a
 *    `useEffect` keyed on `[branchId, departmentId]` (only once both are
 *    concretely known -- i.e. after a successful load) and its returned
 *    unsubscribe function is called from that same effect's cleanup, so
 *    changing either filter or unmounting this page always closes the prior
 *    socket before a new one (if any) is opened. No auto-reconnect is
 *    attempted if the socket drops -- see `queueService.ts`'s own doc
 *    comment on `subscribeToQueueBoard` for why that's a documented scope
 *    boundary rather than an oversight; clicking "Refresh" re-loads the
 *    snapshot and re-opens a fresh subscription.
 */
export default function QueueBoardPage(): JSX.Element {
  const { user } = useAuth();
  const role = user?.role;
  const canManage = role === "front_desk" || role === "nurse" || role === "system_admin";

  const [branchId, setBranchId] = useState<string>("");
  const [departmentIdInput, setDepartmentIdInput] = useState<string>("");
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  const [activeDepartmentId, setActiveDepartmentId] = useState<number | null>(null);

  const [tokens, setTokens] = useState<QueueToken[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  const [appointmentId, setAppointmentId] = useState<string>("");
  const [checkInError, setCheckInError] = useState<string | null>(null);
  const [isCheckingIn, setIsCheckingIn] = useState<boolean>(false);

  const [rowErrors, setRowErrors] = useState<Record<string, string | null>>({});
  const [pendingTokenId, setPendingTokenId] = useState<string | null>(null);

  const load = async (): Promise<void> => {
    const trimmedBranchId = branchId.trim();
    const parsedDepartmentId = Number(departmentIdInput);
    if (!trimmedBranchId || !departmentIdInput.trim() || !Number.isInteger(parsedDepartmentId)) {
      setLoadError("A branch ID and a whole-number department ID are both required.");
      return;
    }
    setIsLoading(true);
    setLoadError(null);
    try {
      const found = await getQueueBoard(trimmedBranchId, parsedDepartmentId);
      setTokens(found);
      setActiveBranchId(trimmedBranchId);
      setActiveDepartmentId(parsedDepartmentId);
    } catch (error) {
      setLoadError(extractErrorMessage(error, "Could not load the queue board."));
    } finally {
      setIsLoading(false);
    }
  };

  // Subscribes to the live channel only once a branch+department pair has
  // been successfully loaded, and always closes the previous socket first --
  // see the doc comment above and `queueService.ts`'s own comment on
  // `subscribeToQueueBoard` for why there's no auto-reconnect.
  useEffect(() => {
    if (activeBranchId === null || activeDepartmentId === null) {
      return;
    }
    const unsubscribe = subscribeToQueueBoard(activeBranchId, activeDepartmentId, (update) => {
      setTokens((previous) =>
        (previous ?? []).map((item) => (item.id === update.tokenId ? { ...item, ...update } : item)),
      );
    });
    return () => {
      unsubscribe();
    };
  }, [activeBranchId, activeDepartmentId]);

  const handleCheckIn = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!appointmentId.trim()) {
      setCheckInError("Appointment ID is required.");
      return;
    }
    setIsCheckingIn(true);
    setCheckInError(null);
    try {
      const created = await checkInWithAppointment(appointmentId.trim());
      setAppointmentId("");
      // Only splice the new token into the currently-loaded board if it
      // actually belongs to the (branch, department) pair on screen -- a
      // check-in for a different department shouldn't silently appear here.
      if (created.branchId === activeBranchId && created.departmentId === activeDepartmentId) {
        setTokens((previous) => [...(previous ?? []), created]);
      }
    } catch (error) {
      setCheckInError(extractErrorMessage(error, "Could not check in this appointment."));
    } finally {
      setIsCheckingIn(false);
    }
  };

  const handleTransition = async (token: QueueToken, nextStatus: TokenStatus): Promise<void> => {
    setRowErrors((previous) => ({ ...previous, [token.id]: null }));
    setPendingTokenId(token.id);
    try {
      const updated = await updateTokenStatus(token.id, nextStatus);
      setTokens((previous) =>
        (previous ?? []).map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [token.id]: extractErrorMessage(error, "Could not update this token's status."),
      }));
    } finally {
      setPendingTokenId(null);
    }
  };

  return (
    <main className="page-container">
      <h1>Queue board</h1>

      <div className="section-header">
        <h2>Filters</h2>
      </div>
      <div className="patient-form">
        <label className="auth-label" htmlFor="queue-branch-id">
          Branch ID
        </label>
        <input
          id="queue-branch-id"
          className="auth-input"
          type="text"
          placeholder="00000000-0000-0000-0000-000000000000"
          value={branchId}
          onChange={(event) => setBranchId(event.target.value)}
        />
        <p className="field-hint">
          There is no branch-lookup endpoint in scope -- enter the branch&apos;s UUID directly.
        </p>
        <label className="auth-label" htmlFor="queue-department-id">
          Department ID
        </label>
        <input
          id="queue-department-id"
          className="auth-input"
          type="number"
          value={departmentIdInput}
          onChange={(event) => setDepartmentIdInput(event.target.value)}
        />
        <p className="field-hint">
          Required here even though the underlying GET treats it as optional -- the live WebSocket
          channel is scoped to one concrete (branch, department) pair.
        </p>
        <button
          className="button-secondary"
          type="button"
          disabled={isLoading}
          onClick={() => void load()}
        >
          {isLoading ? "Loading..." : "Refresh"}
        </button>
      </div>

      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}

      {tokens === null && (
        <p className="empty-state">
          Enter a branch ID and department ID above, then click "Refresh" to view the live queue.
        </p>
      )}

      {canManage && (
        <>
          <div className="section-header">
            <h2>Check in</h2>
          </div>
          <form className="patient-form" onSubmit={(event) => void handleCheckIn(event)}>
            <label className="auth-label" htmlFor="queue-appointment-id">
              Appointment ID
            </label>
            <input
              id="queue-appointment-id"
              className="auth-input"
              type="text"
              placeholder="00000000-0000-0000-0000-000000000000"
              value={appointmentId}
              onChange={(event) => setAppointmentId(event.target.value)}
              required
            />
            <p className="field-hint">
              Branch and department are derived from the appointment server-side. Walk-in check-in
              (no appointment) is available via `queueService.checkInWalkIn` but is not exposed in
              this page -- see the doc comment on `QueueBoardPage` for why.
            </p>
            {checkInError !== null && (
              <p className="auth-error" role="alert">
                {checkInError}
              </p>
            )}
            <button className="auth-submit" type="submit" disabled={isCheckingIn}>
              {isCheckingIn ? "Checking in..." : "Check in"}
            </button>
          </form>
        </>
      )}

      {tokens !== null && tokens.length === 0 && (
        <p className="empty-state">No tokens are currently queued for this branch/department.</p>
      )}

      {tokens !== null && tokens.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Token #</th>
              <th>Status</th>
              <th>Checked in</th>
              <th>Est. wait (min)</th>
              {canManage && <th>Actions</th>}
            </tr>
          </thead>
          <tbody>
            {tokens.map((token) => {
              const rowError = rowErrors[token.id] ?? null;
              const isPending = pendingTokenId === token.id;
              const nextStatuses = LEGAL_TRANSITIONS[token.status];

              return (
                <tr key={token.id}>
                  <td>{token.tokenNumber}</td>
                  <td>
                    <span className={STATUS_BADGE_CLASS[token.status]}>
                      {STATUS_LABEL[token.status]}
                    </span>
                    {rowError !== null && (
                      <p className="auth-error" role="alert">
                        {rowError}
                      </p>
                    )}
                  </td>
                  <td>{new Date(token.checkedInAt).toLocaleString()}</td>
                  <td>{token.estimatedWaitMinutes ?? "--"}</td>
                  {canManage && (
                    <td>
                      {nextStatuses.map((nextStatus) => (
                        <button
                          key={nextStatus}
                          className={nextStatus === "skipped" ? "button-danger" : "button-secondary"}
                          type="button"
                          disabled={isPending}
                          onClick={() => void handleTransition(token, nextStatus)}
                        >
                          {TRANSITION_LABEL[nextStatus]}
                        </button>
                      ))}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
