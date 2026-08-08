import { useEffect, useState } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import PatientSearchSelect from "../components/selects/PatientSearchSelect";
import StaffSelect from "../components/selects/StaffSelect";
import { useAuth } from "../hooks/useAuth";
import { listNotifications, retryNotification } from "../services/notificationService";
import type { ListNotificationsParams } from "../services/notificationService";
import type { NotificationRecord, NotificationStatus } from "../types";
import { extractErrorMessage } from "../utils/errors";

const STATUS_FILTER_OPTIONS: Array<NotificationStatus | ""> = ["", "queued", "sent", "failed"];

const STATUS_BADGE_CLASS: Record<NotificationStatus, string> = {
  queued: "badge badge-warning",
  sent: "badge badge-success",
  failed: "badge badge-danger",
};

const STATUS_LABEL: Record<NotificationStatus, string> = {
  queued: "Queued",
  sent: "Sent",
  failed: "Failed",
};

/**
 * Staff-facing notification history page -- see
 * PRPs/notification-hub-prp.md "ENDPOINTS". A thin read (+ manual retry)
 * surface over the `notifications` table the consumer worker populates;
 * this page never talks to RabbitMQ or the consumer directly, only the two
 * HTTP endpoints.
 *
 * Role gating (see the PRP's per-endpoint auth column):
 *  - `front_desk`: view only -- no Retry button of any kind, same
 *    "view-only" discipline as `BedMatrixPage.tsx`'s front_desk gating.
 *  - `system_admin`: same list, plus a "Retry" button on any `failed` row
 *    (`PATCH /notifications/{id}/retry` is `system_admin` only per the
 *    PRP's ENDPOINTS table -- front_desk is read-only here, not full CRUD
 *    like it is on some other modules' bed matrix).
 *
 * Judgment calls (documented per FRONTEND-AGENT instructions):
 *  - `branchId` UX for `system_admin` mirrors `BedMatrixPage.tsx`/
 *    `InvoicePage.tsx`: a `<BranchSelect>` dropdown, required before the
 *    list can load. Non-`system_admin` callers never see this input --
 *    their branch is implicit, so `branch_id` is simply never sent, and
 *    (same as `BedMatrixPage.tsx`) the list auto-loads on mount for them.
 *  - **Patient/User ID filters**: `patientIdFilter` is a
 *    `<PatientSearchSelect>` for every role (its backing endpoint has no
 *    role gate beyond "any staff member"). `userIdFilter` is a
 *    `<StaffSelect>` for `system_admin` only -- `GET /api/v1/staff` is
 *    `system_admin`-only server-side (see `directory.py`), so a
 *    non-`system_admin` caller can't use it as a lookup; they keep the
 *    original free-text UUID input for this one field since they have no
 *    accessible way to browse staff.
 *  - **Payload rendering**: `payload` is an arbitrary JSON object whose
 *    shape varies by which of the four event topics triggered the
 *    notification (per the PRP's "SCOPE-DEFINING FACT" -- no bespoke
 *    per-template schema is worth building for four topics). Rendered as a
 *    plain `<pre>` JSON dump rather than a bespoke per-template layout.
 *  - **Retry updates the row in place**: a successful retry replaces that
 *    row with the response body (new `status`, and potentially a
 *    resolved-then-failed-again row still shows `failed`) rather than
 *    forcing a full list reload, same "patch the local list" pattern
 *    `InvoicePage.tsx`'s `applyUpdatedInvoice` uses.
 */
export default function NotificationHistoryPage(): JSX.Element {
  const { user } = useAuth();
  const role = user?.role;
  const isSystemAdmin = role === "system_admin";
  const canRetry = role === "system_admin";

  const [branchId, setBranchId] = useState<string>("");
  const [patientIdFilter, setPatientIdFilter] = useState<string>("");
  const [userIdFilter, setUserIdFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<NotificationStatus | "">("");

  const [notifications, setNotifications] = useState<NotificationRecord[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  const [rowErrors, setRowErrors] = useState<Record<string, string | null>>({});
  const [pendingId, setPendingId] = useState<string | null>(null);

  const load = async (): Promise<void> => {
    if (isSystemAdmin && !branchId.trim()) {
      return;
    }
    setIsLoading(true);
    setLoadError(null);
    try {
      const params: ListNotificationsParams = {
        branchId: isSystemAdmin ? branchId.trim() : undefined,
        patientId: patientIdFilter.trim() || undefined,
        userId: userIdFilter.trim() || undefined,
        status: statusFilter || undefined,
      };
      const found = await listNotifications(params);
      setNotifications(found);
    } catch (error) {
      setLoadError(extractErrorMessage(error, "Could not load notification history."));
    } finally {
      setIsLoading(false);
    }
  };

  // A non-system_admin's branch is implicit -- auto-load on mount, same
  // pattern as BedMatrixPage.tsx. A system_admin has no default branch, so
  // nothing auto-loads for them until a branch ID is entered.
  useEffect(() => {
    if (!isSystemAdmin) {
      void load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSystemAdmin]);

  const handleRetry = async (notification: NotificationRecord): Promise<void> => {
    setRowErrors((previous) => ({ ...previous, [notification.id]: null }));
    setPendingId(notification.id);
    try {
      const updated = await retryNotification(notification.id);
      setNotifications((previous) =>
        (previous ?? []).map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [notification.id]: extractErrorMessage(error, "Could not retry this notification."),
      }));
    } finally {
      setPendingId(null);
    }
  };

  return (
    <main className="page-container">
      <h1>Notification history</h1>

      <div className="section-header">
        <h2>Filters</h2>
      </div>
      <div className="patient-form">
        {isSystemAdmin && (
          <>
            <label className="auth-label" htmlFor="notification-branch-id">
              Branch
            </label>
            <BranchSelect id="notification-branch-id" value={branchId} onChange={setBranchId} />
          </>
        )}
        <label className="auth-label" htmlFor="notification-patient-id">
          Patient (optional)
        </label>
        <PatientSearchSelect
          id="notification-patient-id"
          value={patientIdFilter}
          onChange={setPatientIdFilter}
        />
        <label className="auth-label" htmlFor="notification-user-id">
          User (optional)
        </label>
        {isSystemAdmin ? (
          <StaffSelect
            id="notification-user-id"
            value={userIdFilter}
            onChange={setUserIdFilter}
            branchId={branchId}
          />
        ) : (
          <input
            id="notification-user-id"
            className="auth-input"
            type="text"
            placeholder="00000000-0000-0000-0000-000000000000"
            value={userIdFilter}
            onChange={(event) => setUserIdFilter(event.target.value)}
          />
        )}
        <label className="auth-label" htmlFor="notification-status">
          Status
        </label>
        <select
          id="notification-status"
          className="auth-input"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as NotificationStatus | "")}
        >
          {STATUS_FILTER_OPTIONS.map((status) => (
            <option key={status || "all"} value={status}>
              {status ? STATUS_LABEL[status] : "All statuses"}
            </option>
          ))}
        </select>
        <button
          className="button-secondary"
          type="button"
          disabled={isLoading || (isSystemAdmin && !branchId.trim())}
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

      {isSystemAdmin && notifications === null && (
        <p className="empty-state">Enter a branch ID above, then click "Refresh" to view its notifications.</p>
      )}

      {notifications !== null && notifications.length === 0 && (
        <p className="empty-state">No notifications match the current filters.</p>
      )}

      {notifications !== null && notifications.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Channel</th>
              <th>Template</th>
              <th>Status</th>
              <th>Created</th>
              <th>Payload</th>
              {canRetry && <th>Actions</th>}
            </tr>
          </thead>
          <tbody>
            {notifications.map((notification) => {
              const rowError = rowErrors[notification.id] ?? null;
              const isPending = pendingId === notification.id;

              return (
                <tr key={notification.id}>
                  <td>{notification.channel}</td>
                  <td>{notification.template}</td>
                  <td>
                    <span className={STATUS_BADGE_CLASS[notification.status]}>
                      {STATUS_LABEL[notification.status]}
                    </span>
                    {rowError !== null && (
                      <p className="auth-error" role="alert">
                        {rowError}
                      </p>
                    )}
                  </td>
                  <td>{new Date(notification.createdAt).toLocaleString()}</td>
                  <td>
                    <pre className="payload-dump">{JSON.stringify(notification.payload, null, 2)}</pre>
                  </td>
                  {canRetry && (
                    <td>
                      {notification.status === "failed" && (
                        <button
                          className="button-secondary"
                          type="button"
                          disabled={isPending}
                          onClick={() => void handleRetry(notification)}
                        >
                          {isPending ? "Retrying..." : "Retry"}
                        </button>
                      )}
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
