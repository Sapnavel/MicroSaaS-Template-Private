import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { createLabOrder, getLabOrder } from "../services/labService";
import type { CreateLabOrderData } from "../services/labService";
import type { LabOrder } from "../types";
import { extractErrorMessage } from "../utils/errors";

const emptyForm: CreateLabOrderData = {
  consultationId: "",
  testCode: "",
};

/**
 * Handles both "create a new lab order" (no `:id` in the URL, routed at
 * `/lab/orders/new`) and "view an existing order" (`:id` present, routed at
 * `/lab/orders/:id`) in one component -- the PRP left this split as our
 * call ("consider whether it needs a route param for an existing order id
 * vs. a create-new mode ... a single page handling both is fine").
 *
 * Judgment call: App.tsx gates the two routes with *different*
 * `allowedRoles` even though they render the same component --
 * `/lab/orders/new` is doctor/system_admin only (matches
 * `POST /lab/orders`'s auth), `/lab/orders/:id` is doctor/nurse/lab_tech/
 * system_admin (matches `GET /lab/orders/{id}`'s broader auth), since a
 * nurse or lab_tech may land on this page from a link elsewhere (e.g. the
 * worklist) just to look up a specific order.
 *
 * This page is intentionally read-only once an order exists -- no
 * transition actions live here. Collecting/processing/verifying/attaching
 * all happen on `LabWorklistPage`, which is organized by status/action
 * across many orders; duplicating those actions here would mean two places
 * doing the same state-machine call for no benefit.
 */
export default function LabOrderPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const isCreateMode = !id;

  const [form, setForm] = useState<CreateLabOrderData>(emptyForm);
  const [createError, setCreateError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState<boolean>(false);

  const [order, setOrder] = useState<LabOrder | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) {
      return;
    }
    const load = async (): Promise<void> => {
      try {
        const found = await getLabOrder(id);
        setOrder(found);
      } catch (error) {
        setLoadError(extractErrorMessage(error, "Could not load this lab order."));
      }
    };
    void load();
  }, [id]);

  const handleCreate = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setCreateError(null);
    setIsCreating(true);
    try {
      const created = await createLabOrder(form);
      // Land on the view for the order just created rather than resetting
      // the form -- there's nothing more to fill in for a fresh order.
      navigate(`/lab/orders/${created.id}`, { replace: true });
    } catch (error) {
      setCreateError(extractErrorMessage(error, "Could not create this lab order."));
    } finally {
      setIsCreating(false);
    }
  };

  if (isCreateMode) {
    return (
      <main className="page-container">
        <h1>New lab order</h1>
        <form className="patient-form" onSubmit={(event) => void handleCreate(event)}>
          {createError !== null && (
            <p className="auth-error" role="alert">
              {createError}
            </p>
          )}
          <label className="auth-label" htmlFor="lab-order-consultation-id">
            Consultation ID
          </label>
          <input
            id="lab-order-consultation-id"
            className="auth-input"
            type="text"
            value={form.consultationId}
            onChange={(event) =>
              setForm((previous) => ({ ...previous, consultationId: event.target.value }))
            }
            required
          />
          <label className="auth-label" htmlFor="lab-order-test-code">
            Test code
          </label>
          <input
            id="lab-order-test-code"
            className="auth-input"
            type="text"
            placeholder="e.g. CBC"
            value={form.testCode}
            onChange={(event) => setForm((previous) => ({ ...previous, testCode: event.target.value }))}
            required
          />
          <button className="auth-submit" type="submit" disabled={isCreating}>
            {isCreating ? "Creating..." : "Create lab order"}
          </button>
        </form>
      </main>
    );
  }

  if (loadError !== null) {
    return (
      <main className="page-container">
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      </main>
    );
  }

  if (!order) {
    return (
      <main className="page-container">
        <p className="empty-state">Loading...</p>
      </main>
    );
  }

  const sample = order.sample;

  return (
    <main className="page-container">
      <h1>Lab order</h1>
      <div className="list-item-card">
        <dl>
          <dt>Test code</dt>
          <dd>{order.testCode}</dd>
          <dt>Patient ID</dt>
          <dd>{order.patientId}</dd>
          <dt>Consultation ID</dt>
          <dd>{order.consultationId}</dd>
          <dt>Ordered by</dt>
          <dd>{order.orderedBy}</dd>
          <dt>Status</dt>
          <dd>{order.status}</dd>
          <dt>Created at</dt>
          <dd>{order.createdAt}</dd>
        </dl>
      </div>

      <div className="section-header">
        <h2>Sample</h2>
      </div>
      {sample === null ? (
        <p className="empty-state">No sample collected yet.</p>
      ) : (
        <div className="list-item-card">
          <dl>
            <dt>Collected by</dt>
            <dd>{sample.collectedBy ?? "—"}</dd>
            <dt>Collected at</dt>
            <dd>{sample.collectedAt ?? "—"}</dd>
            <dt>Processed at</dt>
            <dd>{sample.processedAt ?? "—"}</dd>
            <dt>Verified by</dt>
            <dd>{sample.verifiedBy ?? "—"}</dd>
            <dt>Verified at</dt>
            <dd>{sample.verifiedAt ?? "—"}</dd>
            {/* `sample.result` is `undefined` (key absent) for a nurse
                caller -- don't render the row at all in that case, rather
                than showing a misleading blank/"Pending" value for data the
                viewer isn't even entitled to know exists. */}
            {sample.result !== undefined && (
              <>
                <dt>Result</dt>
                <dd>{sample.result ?? "Pending verification"}</dd>
              </>
            )}
            <dt>Attached to record at</dt>
            <dd>{sample.attachedToEmrAt ?? "—"}</dd>
          </dl>
        </div>
      )}
    </main>
  );
}
