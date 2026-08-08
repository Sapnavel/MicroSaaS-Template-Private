import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { getMyBill, type InvoiceDetail } from "../services/patientPortalService";
import { extractErrorMessage } from "../utils/errors";

/** `/me/bills/:id` -- itemized invoice + insurance claim (if any), read-only. */
export default function MyBillDetailPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<InvoiceDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getMyBill(id)
      .then(setDetail)
      .catch((error: unknown) => setLoadError(extractErrorMessage(error, "Could not load this bill.")));
  }, [id]);

  if (loadError !== null) {
    return (
      <main className="page-container">
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      </main>
    );
  }

  if (!detail) {
    return (
      <main className="page-container">
        <p className="empty-state">Loading...</p>
      </main>
    );
  }

  const { invoice, items, claim } = detail;

  return (
    <main className="page-container">
      <h1>Bill</h1>
      <div className="list-item-card">
        <dl>
          <dt>Status</dt>
          <dd>{invoice.status}</dd>
          <dt>Total</dt>
          <dd>${invoice.totalAmount.toFixed(2)}</dd>
          <dt>Date</dt>
          <dd>{new Date(invoice.createdAt).toLocaleString()}</dd>
        </dl>
      </div>

      <div className="section-header">
        <h2>Charges</h2>
      </div>
      {items.length === 0 ? (
        <p className="empty-state">No line items yet.</p>
      ) : (
        <ul className="list-plain">
          {items.map((item) => (
            <li key={item.id} className="list-item-card">
              {item.description} — ${item.amount.toFixed(2)}
            </li>
          ))}
        </ul>
      )}

      {claim !== null && (
        <>
          <div className="section-header">
            <h2>Insurance claim</h2>
          </div>
          <div className="list-item-card">
            <dl>
              <dt>Payer</dt>
              <dd>{claim.payerName}</dd>
              <dt>Claim amount</dt>
              <dd>${claim.claimAmount.toFixed(2)}</dd>
              <dt>Your copay</dt>
              <dd>${claim.patientCopay.toFixed(2)}</dd>
              <dt>Status</dt>
              <dd>{claim.state}</dd>
            </dl>
          </div>
        </>
      )}
    </main>
  );
}
