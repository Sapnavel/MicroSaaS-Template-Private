import { useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../hooks/useAuth";
import {
  addInvoiceItem,
  createInvoice,
  getInvoice,
  listChargeableEvents,
  listPatientInvoices,
  markInvoicePaid,
  splitInvoice,
  voidInvoice,
} from "../services/billingService";
import type { InvoiceDetail } from "../services/billingService";
import type { ChargeableEvent, Invoice, InvoiceStatus, SourceType } from "../types";
import { extractErrorMessage } from "../utils/errors";

const STATUS_BADGE_CLASS: Record<InvoiceStatus, string> = {
  open: "badge badge-primary",
  split: "badge badge-warning",
  paid: "badge badge-success",
  void: "badge badge-danger",
};

const STATUS_LABEL: Record<InvoiceStatus, string> = {
  open: "Open",
  split: "Split",
  paid: "Paid",
  void: "Void",
};

const CLAIM_STATE_BADGE_CLASS: Record<string, string> = {
  submitted: "badge badge-primary",
  adjudicating: "badge badge-warning",
  approved: "badge badge-primary",
  denied: "badge badge-danger",
  paid: "badge badge-success",
};

const SOURCE_TYPE_OPTIONS: SourceType[] = ["consultation", "lab_order", "prescription", "admission"];

const SOURCE_TYPE_LABEL: Record<SourceType, string> = {
  consultation: "Consultation",
  lab_order: "Lab order",
  prescription: "Prescription",
  admission: "Admission",
};

interface AddItemFormState {
  sourceType: SourceType;
  sourceId: string;
  description: string;
  amount: string;
}

const emptyAddItemForm: AddItemFormState = {
  sourceType: "consultation",
  sourceId: "",
  description: "",
  amount: "",
};

interface SplitFormState {
  payerName: string;
  claimAmount: string;
  patientCopay: string;
}

const emptySplitForm: SplitFormState = { payerName: "", claimAmount: "", patientCopay: "" };

type RowAction = "addItem" | "split";

/**
 * Primary billing workflow page -- see PRPs/billing-module-prp.md
 * "ENDPOINTS". A patient ID input loads that patient's invoices and (for
 * `billing_admin`/`system_admin` only -- `front_desk` is not on the
 * `chargeable-events` endpoint's auth list at all) their chargeable
 * events, which double as a picker to pre-fill the "add item" form.
 *
 * Role gating (see the PRP's per-endpoint auth column):
 *  - `front_desk`: view only (invoices, expanded item/claim detail) -- no
 *    action buttons of any kind, same "view-only" discipline as
 *    `BedMatrixPage.tsx`'s front_desk gating. Never calls
 *    `listChargeableEvents` (not in its auth list -- would 403).
 *  - `billing_admin` / `system_admin`: every action (create, add item,
 *    split, mark paid, void).
 *
 * Judgment calls (documented per FRONTEND-AGENT instructions):
 *  - `branchId` UX for `system_admin` mirrors `BedMatrixPage.tsx`: a single
 *    free-text branch-ID input, shown only to `system_admin` (no
 *    branch-lookup endpoint exists). A non-`system_admin` caller's branch
 *    is their own `user.branchId` -- sent on every request (list endpoints
 *    require `branch_id` for non-`system_admin` callers per the PRP's
 *    ENDPOINTS table; `POST /invoices` always requires it in the body,
 *    regardless of role).
 *  - **"Split" is only offered on `open` invoices**, not `open`/`split` as
 *    a literal reading of the action-button grouping might suggest --
 *    `split_invoice` 409s unless `invoice.status == "open"` (PRP
 *    "ENGINE DESIGN"), so a button that's guaranteed to fail on an
 *    already-split invoice isn't offered. "Mark paid"/"Void" are offered
 *    on both `open` and `split`, matching `mark_invoice_paid`/
 *    `void_invoice`'s actual accepted states.
 *  - **Add-item source picker**: since this system has no fee schedule
 *    (design decision #6), picking a loaded chargeable event only
 *    pre-fills `source_type`/`source_id`/`description` -- `amount` is
 *    always a required manual entry.
 */
export default function InvoicePage(): JSX.Element {
  const { user } = useAuth();
  const role = user?.role;
  const isSystemAdmin = role === "system_admin";
  const canManage = role === "billing_admin" || role === "system_admin";

  const [branchId, setBranchId] = useState<string>("");
  const [patientId, setPatientId] = useState<string>("");

  const [invoices, setInvoices] = useState<Invoice[] | null>(null);
  const [chargeableEvents, setChargeableEvents] = useState<ChargeableEvent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  const [createError, setCreateError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState<boolean>(false);

  const [expandedInvoiceId, setExpandedInvoiceId] = useState<string | null>(null);
  const [detailByInvoiceId, setDetailByInvoiceId] = useState<Record<string, InvoiceDetail>>({});
  const [detailError, setDetailError] = useState<Record<string, string | null>>({});
  const [isDetailLoading, setIsDetailLoading] = useState<string | null>(null);

  const [openAction, setOpenAction] = useState<{ invoiceId: string; action: RowAction } | null>(null);
  const [addItemForm, setAddItemForm] = useState<AddItemFormState>(emptyAddItemForm);
  const [splitForm, setSplitForm] = useState<SplitFormState>(emptySplitForm);

  const [rowErrors, setRowErrors] = useState<Record<string, string | null>>({});
  const [pendingInvoiceId, setPendingInvoiceId] = useState<string | null>(null);

  const effectiveBranchId = isSystemAdmin ? branchId.trim() : user?.branchId ?? "";
  const canLoad = patientId.trim().length > 0 && (!isSystemAdmin || effectiveBranchId.length > 0);

  const load = async (): Promise<void> => {
    if (!canLoad) {
      return;
    }
    setIsLoading(true);
    setLoadError(null);
    try {
      const trimmedPatientId = patientId.trim();
      const [foundInvoices, foundEvents] = await Promise.all([
        listPatientInvoices(trimmedPatientId, { branchId: effectiveBranchId || undefined }),
        canManage
          ? listChargeableEvents(trimmedPatientId, { branchId: effectiveBranchId || undefined })
          : Promise.resolve<ChargeableEvent[]>([]),
      ]);
      setInvoices(foundInvoices);
      setChargeableEvents(canManage ? foundEvents : null);
      setExpandedInvoiceId(null);
      setDetailByInvoiceId({});
    } catch (error) {
      setLoadError(extractErrorMessage(error, "Could not load this patient's billing information."));
    } finally {
      setIsLoading(false);
    }
  };

  const loadDetail = async (invoiceId: string): Promise<void> => {
    setIsDetailLoading(invoiceId);
    setDetailError((previous) => ({ ...previous, [invoiceId]: null }));
    try {
      const detail = await getInvoice(invoiceId);
      setDetailByInvoiceId((previous) => ({ ...previous, [invoiceId]: detail }));
    } catch (error) {
      setDetailError((previous) => ({
        ...previous,
        [invoiceId]: extractErrorMessage(error, "Could not load this invoice's details."),
      }));
    } finally {
      setIsDetailLoading(null);
    }
  };

  const toggleExpand = (invoiceId: string): void => {
    if (expandedInvoiceId === invoiceId) {
      setExpandedInvoiceId(null);
      return;
    }
    setExpandedInvoiceId(invoiceId);
    if (!detailByInvoiceId[invoiceId]) {
      void loadDetail(invoiceId);
    }
  };

  const refreshChargeableEvents = async (): Promise<void> => {
    if (!canManage) {
      return;
    }
    try {
      const foundEvents = await listChargeableEvents(patientId.trim(), {
        branchId: effectiveBranchId || undefined,
      });
      setChargeableEvents(foundEvents);
    } catch {
      // Non-fatal -- the mutation itself already succeeded; the chargeable
      // events list simply won't reflect this change until the next "Load".
    }
  };

  const applyUpdatedInvoice = (updated: Invoice): void => {
    setInvoices((previous) => (previous ?? []).map((item) => (item.id === updated.id ? updated : item)));
  };

  const handleCreateInvoice = async (): Promise<void> => {
    if (!canLoad) {
      return;
    }
    setIsCreating(true);
    setCreateError(null);
    try {
      const created = await createInvoice({ patientId: patientId.trim(), branchId: effectiveBranchId });
      setInvoices((previous) => [created, ...(previous ?? [])]);
    } catch (error) {
      setCreateError(extractErrorMessage(error, "Could not create a new invoice."));
    } finally {
      setIsCreating(false);
    }
  };

  const closeRowAction = (): void => {
    setOpenAction(null);
    setAddItemForm(emptyAddItemForm);
    setSplitForm(emptySplitForm);
  };

  const openRowAction = (invoiceId: string, action: RowAction): void => {
    setRowErrors((previous) => ({ ...previous, [invoiceId]: null }));
    setOpenAction({ invoiceId, action });
    if (action === "addItem") {
      setAddItemForm(emptyAddItemForm);
    } else {
      setSplitForm(emptySplitForm);
    }
  };

  const handleChargeableEventPick = (value: string): void => {
    if (!value) {
      return;
    }
    const separatorIndex = value.indexOf("::");
    const sourceType = value.slice(0, separatorIndex) as SourceType;
    const sourceId = value.slice(separatorIndex + 2);
    const picked = (chargeableEvents ?? []).find(
      (item) => item.sourceType === sourceType && item.sourceId === sourceId,
    );
    setAddItemForm((previous) => ({
      ...previous,
      sourceType,
      sourceId,
      description: picked?.suggestedDescription ?? previous.description,
    }));
  };

  const handleAddItemSubmit = async (invoice: Invoice, event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const amount = Number(addItemForm.amount);
    if (
      !addItemForm.sourceId.trim() ||
      !addItemForm.description.trim() ||
      !Number.isFinite(amount) ||
      amount <= 0
    ) {
      setRowErrors((previous) => ({
        ...previous,
        [invoice.id]: "Source ID, description, and a positive amount are all required.",
      }));
      return;
    }
    setPendingInvoiceId(invoice.id);
    try {
      const updated = await addInvoiceItem(invoice.id, {
        sourceType: addItemForm.sourceType,
        sourceId: addItemForm.sourceId.trim(),
        description: addItemForm.description.trim(),
        amount,
      });
      applyUpdatedInvoice(updated);
      closeRowAction();
      setExpandedInvoiceId(invoice.id);
      await Promise.all([loadDetail(invoice.id), refreshChargeableEvents()]);
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [invoice.id]: extractErrorMessage(error, "Could not add this item to the invoice."),
      }));
    } finally {
      setPendingInvoiceId(null);
    }
  };

  const handleSplitSubmit = async (invoice: Invoice, event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const claimAmount = Number(splitForm.claimAmount);
    const patientCopay = Number(splitForm.patientCopay);
    if (
      !splitForm.payerName.trim() ||
      !Number.isFinite(claimAmount) ||
      claimAmount < 0 ||
      !Number.isFinite(patientCopay) ||
      patientCopay < 0
    ) {
      setRowErrors((previous) => ({
        ...previous,
        [invoice.id]: "Payer name, claim amount, and patient copay are all required.",
      }));
      return;
    }
    setPendingInvoiceId(invoice.id);
    try {
      const updated = await splitInvoice(invoice.id, {
        payerName: splitForm.payerName.trim(),
        claimAmount,
        patientCopay,
      });
      applyUpdatedInvoice(updated);
      closeRowAction();
      setExpandedInvoiceId(invoice.id);
      await loadDetail(invoice.id);
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [invoice.id]: extractErrorMessage(
          error,
          "Could not split this invoice -- confirm claim amount plus patient copay equals the total.",
        ),
      }));
    } finally {
      setPendingInvoiceId(null);
    }
  };

  const handleMarkPaid = async (invoice: Invoice): Promise<void> => {
    setRowErrors((previous) => ({ ...previous, [invoice.id]: null }));
    setPendingInvoiceId(invoice.id);
    try {
      const updated = await markInvoicePaid(invoice.id);
      applyUpdatedInvoice(updated);
      if (expandedInvoiceId === invoice.id) {
        await loadDetail(invoice.id);
      }
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [invoice.id]: extractErrorMessage(error, "Could not mark this invoice as paid."),
      }));
    } finally {
      setPendingInvoiceId(null);
    }
  };

  const handleVoid = async (invoice: Invoice): Promise<void> => {
    setRowErrors((previous) => ({ ...previous, [invoice.id]: null }));
    setPendingInvoiceId(invoice.id);
    try {
      const updated = await voidInvoice(invoice.id);
      applyUpdatedInvoice(updated);
      if (expandedInvoiceId === invoice.id) {
        await loadDetail(invoice.id);
      }
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [invoice.id]: extractErrorMessage(error, "Could not void this invoice."),
      }));
    } finally {
      setPendingInvoiceId(null);
    }
  };

  return (
    <main className="page-container">
      <h1>Invoices</h1>

      <div className="section-header">
        <h2>Load a patient's billing information</h2>
      </div>
      <div className="patient-form">
        {isSystemAdmin && (
          <>
            <label className="auth-label" htmlFor="invoice-branch-id">
              Branch ID
            </label>
            <input
              id="invoice-branch-id"
              className="auth-input"
              type="text"
              placeholder="00000000-0000-0000-0000-000000000000"
              value={branchId}
              onChange={(event) => setBranchId(event.target.value)}
            />
            <p className="field-hint">
              There is no branch-lookup endpoint in scope -- enter the branch&apos;s UUID directly.
            </p>
          </>
        )}
        <label className="auth-label" htmlFor="invoice-patient-id">
          Patient ID (UUID)
        </label>
        <input
          id="invoice-patient-id"
          className="auth-input"
          type="text"
          placeholder="00000000-0000-0000-0000-000000000000"
          value={patientId}
          onChange={(event) => setPatientId(event.target.value)}
        />
        <div className="conflict-actions">
          <button className="button-secondary" type="button" disabled={isLoading || !canLoad} onClick={() => void load()}>
            {isLoading ? "Loading..." : "Load"}
          </button>
          {canManage && (
            <button
              className="auth-submit"
              type="button"
              disabled={isCreating || !canLoad}
              onClick={() => void handleCreateInvoice()}
            >
              {isCreating ? "Creating..." : "Create invoice"}
            </button>
          )}
        </div>
      </div>

      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}
      {createError !== null && (
        <p className="auth-error" role="alert">
          {createError}
        </p>
      )}

      {invoices === null && (
        <p className="empty-state">Enter a patient ID above, then click "Load" to view their invoices.</p>
      )}

      {invoices !== null && invoices.length === 0 && (
        <p className="empty-state">This patient has no invoices yet.</p>
      )}

      {invoices !== null && invoices.length > 0 && (
        <ul className="list-plain">
          {invoices.map((invoice) => {
            const rowError = rowErrors[invoice.id] ?? null;
            const isPending = pendingInvoiceId === invoice.id;
            const isExpanded = expandedInvoiceId === invoice.id;
            const isOpenFor = (action: RowAction): boolean =>
              openAction?.invoiceId === invoice.id && openAction.action === action;
            const detail = detailByInvoiceId[invoice.id];
            const thisDetailError = detailError[invoice.id] ?? null;

            return (
              <li key={invoice.id} className="list-item-card">
                <div className="bed-card-header">
                  <h3>Invoice {invoice.id}</h3>
                  <span className={STATUS_BADGE_CLASS[invoice.status]}>{STATUS_LABEL[invoice.status]}</span>
                </div>
                <p>
                  Total: <strong>{invoice.totalAmount.toFixed(2)}</strong> -- created{" "}
                  {new Date(invoice.createdAt).toLocaleString()}
                </p>

                {rowError !== null && (
                  <p className="auth-error" role="alert">
                    {rowError}
                  </p>
                )}

                <div className="conflict-actions">
                  <button className="button-secondary" type="button" onClick={() => toggleExpand(invoice.id)}>
                    {isExpanded ? "Hide details" : "View details"}
                  </button>
                  {canManage && invoice.status === "open" && !isOpenFor("addItem") && (
                    <button
                      className="button-secondary"
                      type="button"
                      disabled={isPending}
                      onClick={() => openRowAction(invoice.id, "addItem")}
                    >
                      Add item
                    </button>
                  )}
                  {canManage && invoice.status === "open" && !isOpenFor("split") && (
                    <button
                      className="button-secondary"
                      type="button"
                      disabled={isPending}
                      onClick={() => openRowAction(invoice.id, "split")}
                    >
                      Split
                    </button>
                  )}
                  {canManage && (invoice.status === "open" || invoice.status === "split") && (
                    <button
                      className="button-secondary"
                      type="button"
                      disabled={isPending}
                      onClick={() => void handleMarkPaid(invoice)}
                    >
                      Mark paid
                    </button>
                  )}
                  {canManage && (invoice.status === "open" || invoice.status === "split") && (
                    <button
                      className="button-danger"
                      type="button"
                      disabled={isPending}
                      onClick={() => void handleVoid(invoice)}
                    >
                      Void
                    </button>
                  )}
                </div>

                {isOpenFor("addItem") && (
                  <form
                    className="override-reason-form"
                    onSubmit={(event) => void handleAddItemSubmit(invoice, event)}
                  >
                    {chargeableEvents !== null && chargeableEvents.length > 0 && (
                      <>
                        <label className="auth-label" htmlFor={`add-item-pick-${invoice.id}`}>
                          Pick a chargeable event (optional)
                        </label>
                        <select
                          id={`add-item-pick-${invoice.id}`}
                          className="auth-input"
                          defaultValue=""
                          onChange={(event) => handleChargeableEventPick(event.target.value)}
                        >
                          <option value="">Enter manually...</option>
                          {chargeableEvents.map((chargeableEvent) => (
                            <option
                              key={`${chargeableEvent.sourceType}::${chargeableEvent.sourceId}`}
                              value={`${chargeableEvent.sourceType}::${chargeableEvent.sourceId}`}
                            >
                              {SOURCE_TYPE_LABEL[chargeableEvent.sourceType]} --{" "}
                              {chargeableEvent.suggestedDescription} (
                              {new Date(chargeableEvent.eventDate).toLocaleDateString()})
                            </option>
                          ))}
                        </select>
                      </>
                    )}
                    <label className="auth-label" htmlFor={`add-item-source-type-${invoice.id}`}>
                      Source type
                    </label>
                    <select
                      id={`add-item-source-type-${invoice.id}`}
                      className="auth-input"
                      value={addItemForm.sourceType}
                      onChange={(event) =>
                        setAddItemForm((previous) => ({
                          ...previous,
                          sourceType: event.target.value as SourceType,
                        }))
                      }
                    >
                      {SOURCE_TYPE_OPTIONS.map((option) => (
                        <option key={option} value={option}>
                          {SOURCE_TYPE_LABEL[option]}
                        </option>
                      ))}
                    </select>
                    <label className="auth-label" htmlFor={`add-item-source-id-${invoice.id}`}>
                      Source ID (UUID)
                    </label>
                    <input
                      id={`add-item-source-id-${invoice.id}`}
                      className="auth-input"
                      type="text"
                      value={addItemForm.sourceId}
                      onChange={(event) =>
                        setAddItemForm((previous) => ({ ...previous, sourceId: event.target.value }))
                      }
                      required
                    />
                    <label className="auth-label" htmlFor={`add-item-description-${invoice.id}`}>
                      Description
                    </label>
                    <input
                      id={`add-item-description-${invoice.id}`}
                      className="auth-input"
                      type="text"
                      value={addItemForm.description}
                      onChange={(event) =>
                        setAddItemForm((previous) => ({ ...previous, description: event.target.value }))
                      }
                      required
                    />
                    <label className="auth-label" htmlFor={`add-item-amount-${invoice.id}`}>
                      Amount
                    </label>
                    <input
                      id={`add-item-amount-${invoice.id}`}
                      className="auth-input"
                      type="number"
                      min="0.01"
                      step="0.01"
                      value={addItemForm.amount}
                      onChange={(event) =>
                        setAddItemForm((previous) => ({ ...previous, amount: event.target.value }))
                      }
                      required
                    />
                    <p className="field-hint">
                      There is no fee schedule in this system -- enter the amount manually.
                    </p>
                    <div className="conflict-actions">
                      <button className="auth-submit" type="submit" disabled={isPending}>
                        {isPending ? "Adding..." : "Confirm add item"}
                      </button>
                      <button
                        className="button-secondary"
                        type="button"
                        disabled={isPending}
                        onClick={closeRowAction}
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                )}

                {isOpenFor("split") && (
                  <form
                    className="override-reason-form"
                    onSubmit={(event) => void handleSplitSubmit(invoice, event)}
                  >
                    <label className="auth-label" htmlFor={`split-payer-${invoice.id}`}>
                      Payer name
                    </label>
                    <input
                      id={`split-payer-${invoice.id}`}
                      className="auth-input"
                      type="text"
                      value={splitForm.payerName}
                      onChange={(event) =>
                        setSplitForm((previous) => ({ ...previous, payerName: event.target.value }))
                      }
                      required
                    />
                    <label className="auth-label" htmlFor={`split-claim-amount-${invoice.id}`}>
                      Claim amount
                    </label>
                    <input
                      id={`split-claim-amount-${invoice.id}`}
                      className="auth-input"
                      type="number"
                      min="0"
                      step="0.01"
                      value={splitForm.claimAmount}
                      onChange={(event) =>
                        setSplitForm((previous) => ({ ...previous, claimAmount: event.target.value }))
                      }
                      required
                    />
                    <label className="auth-label" htmlFor={`split-copay-${invoice.id}`}>
                      Patient copay
                    </label>
                    <input
                      id={`split-copay-${invoice.id}`}
                      className="auth-input"
                      type="number"
                      min="0"
                      step="0.01"
                      value={splitForm.patientCopay}
                      onChange={(event) =>
                        setSplitForm((previous) => ({ ...previous, patientCopay: event.target.value }))
                      }
                      required
                    />
                    <p className="field-hint">
                      Claim amount plus patient copay must exactly equal the invoice total (
                      {invoice.totalAmount.toFixed(2)}).
                    </p>
                    <div className="conflict-actions">
                      <button className="auth-submit" type="submit" disabled={isPending}>
                        {isPending ? "Splitting..." : "Confirm split"}
                      </button>
                      <button
                        className="button-secondary"
                        type="button"
                        disabled={isPending}
                        onClick={closeRowAction}
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                )}

                {isExpanded && (
                  <div className="override-reason-form">
                    {isDetailLoading === invoice.id && <p className="empty-state">Loading details...</p>}
                    {thisDetailError !== null && (
                      <p className="auth-error" role="alert">
                        {thisDetailError}
                      </p>
                    )}
                    {detail && (
                      <>
                        <h4>Items</h4>
                        {detail.items.length === 0 ? (
                          <p className="empty-state">No items on this invoice yet.</p>
                        ) : (
                          <table className="data-table">
                            <thead>
                              <tr>
                                <th>Source</th>
                                <th>Description</th>
                                <th>Amount</th>
                              </tr>
                            </thead>
                            <tbody>
                              {detail.items.map((item) => (
                                <tr key={item.id}>
                                  <td>{SOURCE_TYPE_LABEL[item.sourceType]}</td>
                                  <td>{item.description}</td>
                                  <td>{item.amount.toFixed(2)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                        <h4>Insurance claim</h4>
                        {detail.claim === null ? (
                          <p className="empty-state">This invoice has not been split with an insurer.</p>
                        ) : (
                          <p>
                            {detail.claim.payerName} --{" "}
                            <span className={CLAIM_STATE_BADGE_CLASS[detail.claim.state]}>
                              {detail.claim.state}
                            </span>
                            <br />
                            Claim amount: {detail.claim.claimAmount.toFixed(2)}, patient copay:{" "}
                            {detail.claim.patientCopay.toFixed(2)}
                          </p>
                        )}
                      </>
                    )}
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
