import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listMyBills } from "../services/patientPortalService";
import type { Invoice } from "../types";
import { extractErrorMessage } from "../utils/errors";

/** `/me/bills` -- a patient's own invoices across every branch. */
export default function MyBillsPage(): JSX.Element {
  const [invoices, setInvoices] = useState<Invoice[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    listMyBills()
      .then(setInvoices)
      .catch((error: unknown) => {
        setLoadError(extractErrorMessage(error, "Could not load your bills."));
        setInvoices([]);
      });
  }, []);

  return (
    <main className="page-container">
      <h1>My bills</h1>
      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}
      {invoices === null ? (
        <p className="empty-state">Loading...</p>
      ) : invoices.length === 0 ? (
        <p className="empty-state">No bills on file yet.</p>
      ) : (
        <ul className="list-plain">
          {invoices.map((invoice) => (
            <li key={invoice.id} className="list-item-card">
              <Link to={`/me/bills/${invoice.id}`}>
                {new Date(invoice.createdAt).toLocaleDateString()} — ${invoice.totalAmount.toFixed(2)} (
                {invoice.status})
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
