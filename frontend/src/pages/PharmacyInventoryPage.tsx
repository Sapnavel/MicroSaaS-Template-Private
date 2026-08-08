import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import DrugSelect from "../components/selects/DrugSelect";
import { useAuth } from "../hooks/useAuth";
import {
  createOrUpdateItem,
  getExpiring,
  getLowStock,
  receiveBatch,
  type ReceiveBatchData,
} from "../services/pharmacyService";
import type { ExpiringBatch, InventoryBatch, LowStockItem } from "../types";
import { extractErrorMessage } from "../utils/errors";

const DEFAULT_EXPIRING_WINDOW_DAYS = 30;

interface ReceiveBatchFormState {
  drugId: string;
  batchNumber: string;
  quantity: string;
  expiryDate: string;
}

const emptyBatchForm: ReceiveBatchFormState = {
  drugId: "",
  batchNumber: "",
  quantity: "",
  expiryDate: "",
};

interface ReorderFormState {
  drugId: string;
  reorderThreshold: string;
}

const emptyReorderForm: ReorderFormState = { drugId: "", reorderThreshold: "" };

/**
 * Pharmacy inventory management: set a drug's reorder threshold, receive a
 * new batch of stock, and view the low-stock / expiring-soon alert panels
 * for a branch -- see PRPs/pharmacy-module-prp.md "ENDPOINTS" and
 * "FILES TO CREATE".
 *
 * Judgment calls (documented per FRONTEND-AGENT instructions):
 *  - `system_admin` branch UX: a single `<BranchSelect>` at the top of the
 *    page drives every write (reorder threshold, receive batch) and both
 *    alert panels below, on the premise that a system_admin visiting this
 *    page is administering one branch at a time. A `pharmacist` never sees
 *    this input at all -- their branch is implicit from `user.branchId`,
 *    and the service layer omits the `branch_id` wire key entirely for
 *    them. `drugId` on both forms is a `<DrugSelect>` in place of the old
 *    raw-UUID text box (frontend UUID-to-dropdown conversion follow-up).
 *  - Default alert view: a `pharmacist`'s low-stock/expiring panels
 *    auto-load on mount for their own branch. A `system_admin` sees an
 *    "enter a branch ID above to view" prompt instead of an automatic
 *    "all branches" load (no such bulk endpoint exists, and defaulting to
 *    nothing would be misleading) -- they load a branch's alerts explicitly
 *    via the "Load alerts" button once they've typed a branch ID.
 *  - The reorder-threshold ("set item") form and the receive-batch form are
 *    both on this page since both are inventory-configuration concerns
 *    (contrasted with dispensing, which is patient-facing and lives on
 *    `PharmacyDispensePage.tsx`); the PRP's endpoint list includes both
 *    `POST /pharmacy/items` and `POST /pharmacy/batches`, so both are wired
 *    up even though the PRP's page description foregrounds the batch form.
 */
export default function PharmacyInventoryPage(): JSX.Element {
  const { user } = useAuth();
  const isSystemAdmin = user?.role === "system_admin";

  const [branchId, setBranchId] = useState<string>("");

  const [reorderForm, setReorderForm] = useState<ReorderFormState>(emptyReorderForm);
  const [reorderError, setReorderError] = useState<string | null>(null);
  const [reorderSuccess, setReorderSuccess] = useState<string | null>(null);
  const [isSavingReorder, setIsSavingReorder] = useState<boolean>(false);

  const [batchForm, setBatchForm] = useState<ReceiveBatchFormState>(emptyBatchForm);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [receivedBatch, setReceivedBatch] = useState<InventoryBatch | null>(null);
  const [isReceivingBatch, setIsReceivingBatch] = useState<boolean>(false);

  const [withinDays, setWithinDays] = useState<string>(String(DEFAULT_EXPIRING_WINDOW_DAYS));
  const [lowStock, setLowStock] = useState<LowStockItem[] | null>(null);
  const [lowStockError, setLowStockError] = useState<string | null>(null);
  const [expiring, setExpiring] = useState<ExpiringBatch[] | null>(null);
  const [expiringError, setExpiringError] = useState<string | null>(null);
  const [isLoadingAlerts, setIsLoadingAlerts] = useState<boolean>(false);

  const loadAlerts = async (branchIdOverride?: string): Promise<void> => {
    const effectiveBranchId = isSystemAdmin ? branchIdOverride ?? branchId : undefined;
    if (isSystemAdmin && !effectiveBranchId) {
      return;
    }
    setIsLoadingAlerts(true);
    setLowStockError(null);
    setExpiringError(null);
    const parsedWithinDays = Number(withinDays);
    const effectiveWithinDays = Number.isInteger(parsedWithinDays) && parsedWithinDays > 0 ? parsedWithinDays : undefined;
    try {
      setLowStock(await getLowStock(effectiveBranchId));
    } catch (error) {
      setLowStockError(extractErrorMessage(error, "Could not load low-stock alerts."));
    }
    try {
      setExpiring(await getExpiring(effectiveWithinDays, effectiveBranchId));
    } catch (error) {
      setExpiringError(extractErrorMessage(error, "Could not load expiring-batch alerts."));
    }
    setIsLoadingAlerts(false);
  };

  // A pharmacist's branch is implicit -- auto-load their own branch's
  // alerts on mount. A system_admin has no default branch, so no
  // auto-fetch happens here; see the doc comment above.
  useEffect(() => {
    if (!isSystemAdmin) {
      void loadAlerts();
    }
    // Only ever run once on mount for a pharmacist -- re-running on every
    // `withinDays` keystroke would be noisy; the "Load alerts" button
    // covers manual refresh for everyone.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSystemAdmin]);

  const handleReorderSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setReorderError(null);
    setReorderSuccess(null);
    if (!reorderForm.drugId.trim()) {
      setReorderError("Drug ID is required.");
      return;
    }
    const threshold = Number(reorderForm.reorderThreshold);
    if (!Number.isInteger(threshold) || threshold < 0) {
      setReorderError("Reorder threshold must be a whole number, 0 or greater.");
      return;
    }
    if (isSystemAdmin && !branchId.trim()) {
      setReorderError("Branch ID is required for a system admin.");
      return;
    }
    setIsSavingReorder(true);
    try {
      await createOrUpdateItem({
        drugId: reorderForm.drugId.trim(),
        reorderThreshold: threshold,
        branchId: isSystemAdmin ? branchId.trim() : undefined,
      });
      setReorderSuccess("Reorder threshold saved.");
      setReorderForm(emptyReorderForm);
    } catch (error) {
      setReorderError(extractErrorMessage(error, "Could not save this reorder threshold."));
    } finally {
      setIsSavingReorder(false);
    }
  };

  const handleBatchSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setBatchError(null);
    setReceivedBatch(null);
    if (!batchForm.drugId.trim() || !batchForm.batchNumber.trim() || !batchForm.expiryDate) {
      setBatchError("Drug ID, batch number, and expiry date are all required.");
      return;
    }
    const quantity = Number(batchForm.quantity);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setBatchError("Quantity must be a positive whole number.");
      return;
    }
    if (isSystemAdmin && !branchId.trim()) {
      setBatchError("Branch ID is required for a system admin.");
      return;
    }
    const data: ReceiveBatchData = {
      drugId: batchForm.drugId.trim(),
      batchNumber: batchForm.batchNumber.trim(),
      quantity,
      expiryDate: batchForm.expiryDate,
      branchId: isSystemAdmin ? branchId.trim() : undefined,
    };
    setIsReceivingBatch(true);
    try {
      const batch = await receiveBatch(data);
      setReceivedBatch(batch);
      setBatchForm(emptyBatchForm);
      // Stock just changed -- refresh the alert panels so a batch that
      // clears a low-stock condition (or a batch received with a
      // near expiry date) is reflected immediately.
      void loadAlerts();
    } catch (error) {
      setBatchError(extractErrorMessage(error, "Could not receive this batch."));
    } finally {
      setIsReceivingBatch(false);
    }
  };

  return (
    <main className="page-container">
      <h1>Pharmacy inventory</h1>

      {isSystemAdmin && (
        <div className="patient-form">
          <label className="auth-label" htmlFor="pharmacy-branch-id">
            Branch
          </label>
          <BranchSelect id="pharmacy-branch-id" value={branchId} onChange={setBranchId} />
          <p className="field-hint">Applies to every form and alert panel below.</p>
        </div>
      )}

      <div className="section-header">
        <h2>Set reorder threshold</h2>
      </div>
      <form className="patient-form" onSubmit={(event) => void handleReorderSubmit(event)}>
        {reorderError !== null && (
          <p className="auth-error" role="alert">
            {reorderError}
          </p>
        )}
        {reorderSuccess !== null && (
          <p className="field-hint">{reorderSuccess}</p>
        )}
        <label className="auth-label" htmlFor="reorder-drug-id">
          Drug
        </label>
        <DrugSelect
          id="reorder-drug-id"
          value={reorderForm.drugId}
          onChange={(value) => setReorderForm((previous) => ({ ...previous, drugId: value }))}
        />
        <label className="auth-label" htmlFor="reorder-threshold">
          Reorder threshold
        </label>
        <input
          id="reorder-threshold"
          className="auth-input"
          type="number"
          min={0}
          value={reorderForm.reorderThreshold}
          onChange={(event) =>
            setReorderForm((previous) => ({ ...previous, reorderThreshold: event.target.value }))
          }
          required
        />
        <button className="auth-submit" type="submit" disabled={isSavingReorder}>
          {isSavingReorder ? "Saving..." : "Save threshold"}
        </button>
      </form>

      <div className="section-header">
        <h2>Receive a batch</h2>
      </div>
      <form className="patient-form" onSubmit={(event) => void handleBatchSubmit(event)}>
        {batchError !== null && (
          <p className="auth-error" role="alert">
            {batchError}
          </p>
        )}
        {receivedBatch !== null && (
          <div className="status-banner status-banner--success">
            <p>
              Received batch <strong>{receivedBatch.batchNumber}</strong> — {receivedBatch.quantity} unit(s),
              expiring {receivedBatch.expiryDate}.
            </p>
          </div>
        )}
        <label className="auth-label" htmlFor="batch-drug-id">
          Drug
        </label>
        <DrugSelect
          id="batch-drug-id"
          value={batchForm.drugId}
          onChange={(value) => setBatchForm((previous) => ({ ...previous, drugId: value }))}
        />
        <label className="auth-label" htmlFor="batch-number">
          Batch number
        </label>
        <input
          id="batch-number"
          className="auth-input"
          type="text"
          value={batchForm.batchNumber}
          onChange={(event) => setBatchForm((previous) => ({ ...previous, batchNumber: event.target.value }))}
          required
        />
        <label className="auth-label" htmlFor="batch-quantity">
          Quantity
        </label>
        <input
          id="batch-quantity"
          className="auth-input"
          type="number"
          min={1}
          value={batchForm.quantity}
          onChange={(event) => setBatchForm((previous) => ({ ...previous, quantity: event.target.value }))}
          required
        />
        <label className="auth-label" htmlFor="batch-expiry-date">
          Expiry date
        </label>
        <input
          id="batch-expiry-date"
          className="auth-input"
          type="date"
          value={batchForm.expiryDate}
          onChange={(event) => setBatchForm((previous) => ({ ...previous, expiryDate: event.target.value }))}
          required
        />
        <button className="auth-submit" type="submit" disabled={isReceivingBatch}>
          {isReceivingBatch ? "Receiving..." : "Receive batch"}
        </button>
      </form>

      <div className="section-header">
        <h2>Alerts</h2>
        <div>
          <label className="auth-label" htmlFor="expiring-within-days">
            Expiring within (days)
          </label>
          <input
            id="expiring-within-days"
            className="auth-input"
            type="number"
            min={1}
            value={withinDays}
            onChange={(event) => setWithinDays(event.target.value)}
          />
        </div>
        <button
          className="button-secondary"
          type="button"
          disabled={isLoadingAlerts || (isSystemAdmin && !branchId.trim())}
          onClick={() => void loadAlerts()}
        >
          {isLoadingAlerts ? "Loading..." : "Load alerts"}
        </button>
      </div>

      {isSystemAdmin && lowStock === null && expiring === null && (
        <p className="empty-state">Enter a branch ID above, then click "Load alerts" to view this branch's stock alerts.</p>
      )}

      <h3>Low stock</h3>
      {lowStockError !== null && (
        <p className="auth-error" role="alert">
          {lowStockError}
        </p>
      )}
      {lowStock === null ? (
        !isSystemAdmin && <p className="empty-state">Loading...</p>
      ) : lowStock.length === 0 ? (
        <p className="empty-state">No items below their reorder threshold.</p>
      ) : (
        <ul className="list-plain">
          {lowStock.map((item) => (
            <li key={item.inventoryItemId} className="list-item-card">
              <strong>{item.drugName}</strong>
              <span className="badge badge-primary">
                {item.currentQuantity} / {item.reorderThreshold}
              </span>
              <p className="field-hint">Drug ID: {item.drugId}</p>
            </li>
          ))}
        </ul>
      )}

      <h3>Expiring soon</h3>
      {expiringError !== null && (
        <p className="auth-error" role="alert">
          {expiringError}
        </p>
      )}
      {expiring === null ? (
        !isSystemAdmin && <p className="empty-state">Loading...</p>
      ) : expiring.length === 0 ? (
        <p className="empty-state">No batches expiring in the selected window.</p>
      ) : (
        <ul className="list-plain">
          {expiring.map((batch) => (
            <li key={batch.batchId} className="list-item-card">
              <strong>{batch.drugName}</strong> — batch {batch.batchNumber}
              <span className="badge">{batch.quantity} unit(s)</span>
              <p className="field-hint">Expires {batch.expiryDate}</p>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
