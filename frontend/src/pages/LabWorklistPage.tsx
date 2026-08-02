import { useEffect, useState } from "react";

import { useAuth } from "../hooks/useAuth";
import { listLabOrders, transitionLabOrder } from "../services/labService";
import type { LabOrder, LabOrderStatus, Role } from "../types";
import { extractErrorMessage } from "../utils/errors";

const STATUS_OPTIONS: LabOrderStatus[] = ["ordered", "collected", "processing", "verified", "attached"];

/**
 * The one transition-action a given (status, role) combination offers, if
 * any -- see PRPs/lab-module-prp.md "Per-transition roles":
 *  - ordered    -> collected:  nurse, lab_tech, system_admin
 *  - collected  -> processing: lab_tech, system_admin
 *  - processing -> verified:   lab_tech, system_admin (requires a `result`)
 *  - verified   -> attached:   lab_tech, system_admin
 *  - attached is terminal -- no action.
 * A doctor never transitions anything and never reaches this page
 * (ProtectedRoute's `allowedRoles` for `/lab/worklist` excludes "doctor").
 * This keeps a nurse viewing an `ordered` row seeing only "Mark collected"
 * and nothing else, and a lab_tech seeing exactly one action per row
 * matching that row's current status.
 */
interface TransitionAction {
  toStatus: Exclude<LabOrderStatus, "ordered">;
  label: string;
  requiresResult: boolean;
}

function getAction(status: LabOrderStatus, role: Role | undefined): TransitionAction | null {
  if (status === "ordered" && (role === "nurse" || role === "lab_tech" || role === "system_admin")) {
    return { toStatus: "collected", label: "Mark collected", requiresResult: false };
  }
  if (status === "collected" && (role === "lab_tech" || role === "system_admin")) {
    return { toStatus: "processing", label: "Start processing", requiresResult: false };
  }
  if (status === "processing" && (role === "lab_tech" || role === "system_admin")) {
    return { toStatus: "verified", label: "Submit result & verify", requiresResult: true };
  }
  if (status === "verified" && (role === "lab_tech" || role === "system_admin")) {
    return { toStatus: "attached", label: "Attach to record", requiresResult: false };
  }
  return null;
}

/**
 * Nurse/lab_tech-facing list of orders in a given status, with the one
 * transition action appropriate to the viewer's role and that row's
 * status -- see PRPs/lab-module-prp.md "FILES TO CREATE".
 *
 * Judgment call on the "verify" result-entry UX: an inline expandable
 * section within the row (a textarea + confirm/cancel), not a modal --
 * this is a worklist meant to be worked through row by row, and a modal
 * would hide the rest of the list while filling it in. Reuses the
 * `.override-reason-form` layout class already established by
 * `PrescriptionPage.tsx`'s override-reason flow, since it's the same
 * "inline reveal a small form under a button" shape.
 */
export default function LabWorklistPage(): JSX.Element {
  const { user } = useAuth();

  const [statusFilter, setStatusFilter] = useState<LabOrderStatus>("ordered");
  const [orders, setOrders] = useState<LabOrder[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [expandedOrderId, setExpandedOrderId] = useState<string | null>(null);
  const [resultDrafts, setResultDrafts] = useState<Record<string, string>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string | null>>({});
  const [pendingOrderId, setPendingOrderId] = useState<string | null>(null);

  useEffect(() => {
    setOrders(null);
    setLoadError(null);
    const load = async (): Promise<void> => {
      try {
        const found = await listLabOrders({ status: statusFilter });
        setOrders(found);
      } catch (error) {
        setLoadError(extractErrorMessage(error, "Could not load the worklist."));
      }
    };
    void load();
  }, [statusFilter]);

  const runTransition = async (
    order: LabOrder,
    toStatus: Exclude<LabOrderStatus, "ordered">,
    result?: string,
  ): Promise<void> => {
    setRowErrors((previous) => ({ ...previous, [order.id]: null }));
    setPendingOrderId(order.id);
    try {
      await transitionLabOrder(order.id, toStatus, result);
      // The transitioned order no longer matches the current status
      // filter, so drop it from the visible list rather than refetching
      // the whole worklist.
      setOrders((previous) => (previous ?? []).filter((item) => item.id !== order.id));
      setExpandedOrderId(null);
      setResultDrafts((previous) => {
        const next = { ...previous };
        delete next[order.id];
        return next;
      });
    } catch (error) {
      // Surface the state-machine rejection detail verbatim (e.g. "cannot
      // mark collected: already collected") rather than a generic message
      // -- these 403/404/409/422s are meaningful, not just network noise.
      setRowErrors((previous) => ({
        ...previous,
        [order.id]: extractErrorMessage(error, "Could not update this order."),
      }));
    } finally {
      setPendingOrderId(null);
    }
  };

  return (
    <main className="page-container">
      <h1>Lab worklist</h1>

      <label className="auth-label" htmlFor="lab-worklist-status">
        Status
      </label>
      <select
        id="lab-worklist-status"
        className="auth-input"
        value={statusFilter}
        onChange={(event) => setStatusFilter(event.target.value as LabOrderStatus)}
      >
        {STATUS_OPTIONS.map((status) => (
          <option key={status} value={status}>
            {status}
          </option>
        ))}
      </select>

      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}

      {orders === null ? (
        <p className="empty-state">Loading...</p>
      ) : orders.length === 0 ? (
        <p className="empty-state">No orders in this status.</p>
      ) : (
        <ul className="list-plain">
          {orders.map((order) => {
            const action = getAction(order.status, user?.role);
            const rowError = rowErrors[order.id] ?? null;
            const isExpanded = expandedOrderId === order.id;
            const isPending = pendingOrderId === order.id;
            const draft = resultDrafts[order.id] ?? "";

            return (
              <li key={order.id} className="list-item-card">
                <strong>{order.testCode}</strong> — patient {order.patientId}, consultation{" "}
                {order.consultationId}
                <p className="field-hint">Ordered at {order.createdAt}</p>

                {rowError !== null && (
                  <p className="auth-error" role="alert">
                    {rowError}
                  </p>
                )}

                {action !== null && !action.requiresResult && (
                  <button
                    className="button-secondary"
                    type="button"
                    disabled={isPending}
                    onClick={() => void runTransition(order, action.toStatus)}
                  >
                    {isPending ? "Updating..." : action.label}
                  </button>
                )}

                {action !== null && action.requiresResult && !isExpanded && (
                  <button
                    className="button-secondary"
                    type="button"
                    disabled={isPending}
                    onClick={() => setExpandedOrderId(order.id)}
                  >
                    {action.label}
                  </button>
                )}

                {action !== null && action.requiresResult && isExpanded && (
                  <div className="override-reason-form">
                    <label className="auth-label" htmlFor={`lab-result-${order.id}`}>
                      Result
                    </label>
                    <textarea
                      id={`lab-result-${order.id}`}
                      className="textarea"
                      rows={3}
                      value={draft}
                      onChange={(event) =>
                        setResultDrafts((previous) => ({ ...previous, [order.id]: event.target.value }))
                      }
                    />
                    <div className="conflict-actions">
                      <button
                        className="auth-submit"
                        type="button"
                        disabled={isPending || draft.trim().length === 0}
                        onClick={() => void runTransition(order, action.toStatus, draft)}
                      >
                        {isPending ? "Submitting..." : "Submit result & verify"}
                      </button>
                      <button
                        className="button-secondary"
                        type="button"
                        disabled={isPending}
                        onClick={() => setExpandedOrderId(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
