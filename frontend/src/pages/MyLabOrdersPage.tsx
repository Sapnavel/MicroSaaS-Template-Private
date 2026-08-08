import { useEffect, useState } from "react";

import { listMyLabOrders } from "../services/patientPortalService";
import type { LabOrder } from "../types";
import { extractErrorMessage } from "../utils/errors";

/** `/me/lab-orders` -- a patient's own lab orders and results. Full results
 * (`sample.result`) are included whenever a sample has been verified --
 * unlike the nurse-facing worklist, a patient reading their own report is
 * never withheld the result (see services/patient_portal_service.py's
 * `list_my_lab_orders` docstring). */
export default function MyLabOrdersPage(): JSX.Element {
  const [orders, setOrders] = useState<LabOrder[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    listMyLabOrders()
      .then(setOrders)
      .catch((error: unknown) => {
        setLoadError(extractErrorMessage(error, "Could not load your lab orders."));
        setOrders([]);
      });
  }, []);

  return (
    <main className="page-container">
      <h1>My lab reports</h1>
      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}
      {orders === null ? (
        <p className="empty-state">Loading...</p>
      ) : orders.length === 0 ? (
        <p className="empty-state">No lab orders on file yet.</p>
      ) : (
        <ul className="list-plain">
          {orders.map((order) => (
            <li key={order.id} className="list-item-card">
              <dl>
                <dt>Test</dt>
                <dd>{order.testCode}</dd>
                <dt>Ordered</dt>
                <dd>{new Date(order.createdAt).toLocaleString()}</dd>
                <dt>Status</dt>
                <dd>{order.status}</dd>
                <dt>Result</dt>
                <dd>{order.sample?.result ?? "Not available yet"}</dd>
              </dl>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
