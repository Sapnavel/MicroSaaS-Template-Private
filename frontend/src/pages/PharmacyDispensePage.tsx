import { useState } from "react";
import type { FormEvent } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import DrugSelect from "../components/selects/DrugSelect";
import { useAuth } from "../hooks/useAuth";
import {
  dispense,
  InsufficientStockError,
  UnknownInventoryItemError,
  type DispenseData,
} from "../services/pharmacyService";
import type { DispenseResult } from "../types";
import { extractErrorMessage } from "../utils/errors";

interface DispenseFormState {
  drugId: string;
  quantity: string;
  reference: string;
}

const emptyForm: DispenseFormState = { drugId: "", quantity: "", reference: "" };

/**
 * Dispense form for the Pharmacy Inventory module -- see
 * PRPs/pharmacy-module-prp.md "ENDPOINTS" / "FEFO DISPENSE ENGINE DESIGN".
 * On success, shows the per-batch FEFO allocation breakdown (which batches
 * were drawn from and how much from each) rather than a generic "dispensed"
 * message, since that breakdown is the meaningful pharmacy information --
 * it's what lets a pharmacist confirm earliest-expiring stock was actually
 * used first and reconcile physical batches against the system.
 *
 * Two distinct, actionable failure states are rendered distinctly:
 *  - `InsufficientStockError` (409): shows the backend's actual `available`/
 *    `requested` numbers ("only 12 available, 30 requested"), not a generic
 *    "insufficient stock" message -- the pharmacist needs the real shortfall
 *    to decide whether to dispense a partial course from a different drug,
 *    order more stock, etc.
 *  - `UnknownInventoryItemError` (404): "this branch has never stocked this
 *    drug" -- distinct remediation from a 409 (receive stock first, vs.
 *    order more of what's already there).
 *
 * Judgment call: `branchId` is a `<BranchSelect>` shown only for
 * `system_admin`, same pattern as `PharmacyInventoryPage.tsx`. `drugId` is a
 * `<DrugSelect>` in place of the old raw-UUID text box (frontend
 * UUID-to-dropdown conversion follow-up).
 */
export default function PharmacyDispensePage(): JSX.Element {
  const { user } = useAuth();
  const isSystemAdmin = user?.role === "system_admin";

  const [branchId, setBranchId] = useState<string>("");
  const [form, setForm] = useState<DispenseFormState>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [insufficientStock, setInsufficientStock] = useState<InsufficientStockError | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [result, setResult] = useState<DispenseResult | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setFormError(null);
    setInsufficientStock(null);
    setResult(null);

    if (!form.drugId.trim()) {
      setFormError("Drug ID is required.");
      return;
    }
    const quantity = Number(form.quantity);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setFormError("Quantity must be a positive whole number.");
      return;
    }
    if (isSystemAdmin && !branchId.trim()) {
      setFormError("Branch ID is required for a system admin.");
      return;
    }

    const data: DispenseData = {
      drugId: form.drugId.trim(),
      quantity,
      reference: form.reference.trim() || undefined,
      branchId: isSystemAdmin ? branchId.trim() : undefined,
    };

    setIsSubmitting(true);
    try {
      const dispensed = await dispense(data);
      setResult(dispensed);
      setForm(emptyForm);
    } catch (error) {
      if (error instanceof InsufficientStockError) {
        setInsufficientStock(error);
      } else if (error instanceof UnknownInventoryItemError) {
        setFormError("This branch has never stocked this drug. Receive a batch for it first.");
      } else {
        setFormError(extractErrorMessage(error, "Could not dispense this drug."));
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="page-container">
      <h1>Dispense</h1>

      <form className="patient-form" onSubmit={(event) => void handleSubmit(event)}>
        {formError !== null && (
          <p className="auth-error" role="alert">
            {formError}
          </p>
        )}

        {insufficientStock !== null && (
          <div className="status-banner status-banner--override" role="alert">
            <h3>Not enough stock</h3>
            <p>
              Only <strong>{insufficientStock.available}</strong> unit(s) available, but{" "}
              <strong>{insufficientStock.requested}</strong> were requested. Nothing was dispensed.
            </p>
          </div>
        )}

        {isSystemAdmin && (
          <>
            <label className="auth-label" htmlFor="dispense-branch-id">
              Branch
            </label>
            <BranchSelect id="dispense-branch-id" value={branchId} onChange={setBranchId} required />
          </>
        )}

        <label className="auth-label" htmlFor="dispense-drug-id">
          Drug
        </label>
        <DrugSelect
          id="dispense-drug-id"
          value={form.drugId}
          onChange={(value) => setForm((previous) => ({ ...previous, drugId: value }))}
        />

        <label className="auth-label" htmlFor="dispense-quantity">
          Quantity
        </label>
        <input
          id="dispense-quantity"
          className="auth-input"
          type="number"
          min={1}
          value={form.quantity}
          onChange={(event) => setForm((previous) => ({ ...previous, quantity: event.target.value }))}
          required
        />

        <label className="auth-label" htmlFor="dispense-reference">
          Reference (optional)
        </label>
        <input
          id="dispense-reference"
          className="auth-input"
          type="text"
          placeholder="e.g. prescription ID"
          value={form.reference}
          onChange={(event) => setForm((previous) => ({ ...previous, reference: event.target.value }))}
        />
        <p className="field-hint">
          Not validated against any table -- stored as free text in the audit log for traceability.
        </p>

        <button className="auth-submit" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Dispensing..." : "Dispense"}
        </button>
      </form>

      {result !== null && (
        <div className="status-banner status-banner--success">
          <h3>Dispensed</h3>
          <p>
            {result.quantityDispensed} unit(s) of drug {result.drugId} dispensed.
          </p>

          <h4>Allocation breakdown (earliest-expiring first)</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>Batch number</th>
                <th>Expiry date</th>
                <th>Quantity taken</th>
              </tr>
            </thead>
            <tbody>
              {result.allocations.map((allocation) => (
                <tr key={allocation.batchId}>
                  <td>{allocation.batchNumber}</td>
                  <td>{allocation.expiryDate}</td>
                  <td>{allocation.quantityTaken}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p>
            <button className="button-secondary" type="button" onClick={() => setResult(null)}>
              Dispense another
            </button>
          </p>
        </div>
      )}
    </main>
  );
}
