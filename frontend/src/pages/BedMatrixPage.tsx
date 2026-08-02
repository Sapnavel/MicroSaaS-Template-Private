import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../hooks/useAuth";
import {
  admitPatient,
  dischargePatient,
  getBedMatrix,
  IllegalBedStatusTransitionError,
  transferPatient,
  updateBedStatus,
} from "../services/wardService";
import type { BedMatrixEntry, BedStatus } from "../types";
import { extractErrorMessage } from "../utils/errors";

const STATUS_FILTER_OPTIONS: Array<BedStatus | ""> = ["", "available", "occupied", "cleaning", "blocked"];

const STATUS_BADGE_CLASS: Record<BedStatus, string> = {
  available: "badge badge-success",
  occupied: "badge badge-primary",
  cleaning: "badge badge-warning",
  blocked: "badge badge-danger",
};

const STATUS_LABEL: Record<BedStatus, string> = {
  available: "Available",
  occupied: "Occupied",
  cleaning: "Cleaning",
  blocked: "Blocked",
};

/**
 * Legal manual (status-update-endpoint) transition targets for a given
 * current status -- see PRPs/ward-bed-ot-module-prp.md `set_bed_status`:
 * cleaning->available, available->blocked, cleaning->blocked,
 * blocked->available. `occupied` is never a legal source OR target here --
 * only `admitPatient`/`dischargePatient` ever touch it.
 */
function legalManualTargets(status: BedStatus): Array<Extract<BedStatus, "available" | "blocked">> {
  if (status === "available") {
    return ["blocked"];
  }
  if (status === "cleaning") {
    return ["available", "blocked"];
  }
  if (status === "blocked") {
    return ["available"];
  }
  return [];
}

interface AdmitFormState {
  patientId: string;
  startTime: string;
}

const emptyAdmitForm: AdmitFormState = { patientId: "", startTime: "" };

interface DischargeFormState {
  admissionId: string;
  dischargeTime: string;
}

const emptyDischargeForm: DischargeFormState = { admissionId: "", dischargeTime: "" };

interface TransferFormState {
  admissionId: string;
  newBedId: string;
}

const emptyTransferForm: TransferFormState = { admissionId: "", newBedId: "" };

type RowAction = "admit" | "discharge" | "transfer";

/**
 * Live bed matrix -- see PRPs/ward-bed-ot-module-prp.md "ENDPOINTS" /
 * "FILES TO CREATE". Beds are grouped by ward (a hospital-floor mental
 * model matches how staff actually think about beds) and rendered as a
 * card grid, one card per bed, with a colored status badge reusing the
 * exact palette `.status-banner--success` / `.status-banner--override` /
 * `.status-banner--block` already established elsewhere in this codebase
 * (green=available, blue=occupied — the existing `.badge-primary` —,
 * yellow=cleaning, red=blocked) rather than inventing a new visual
 * language, per FRONTEND-AGENT instructions.
 *
 * Role gating (see the PRP's per-endpoint auth column):
 *  - `front_desk`: view only, no action buttons of any kind.
 *  - `nurse` / `system_admin`: admit/discharge/transfer AND the manual
 *    status-change actions (mark available/blocked).
 *  - `doctor`: admit/discharge/transfer but NOT the manual status-change
 *    actions (only a nurse marks a bed clean/blocked).
 *
 * Judgment calls (documented per FRONTEND-AGENT instructions):
 *  - `branchId` UX for `system_admin` mirrors `PharmacyInventoryPage.tsx`:
 *    a single free-text branch-ID input (no "list branches" endpoint
 *    exists), and the matrix doesn't auto-load until one is entered.
 *    Non-`system_admin` callers never see this input -- their branch is
 *    implicit, so `branch_id` is simply never sent, same as Pharmacy.
 *  - **Admission-ID gap**: `GET /wards/beds` deliberately does NOT return an
 *    admission id per the PRP's contract (a bed row is `{id, ward_id,
 *    ward_name, branch_id, label, status}`), but `discharge`/`transfer`
 *    are keyed by admission id, not bed id. There is no "get the active
 *    admission for this bed" endpoint in scope. This page tracks admission
 *    ids it learns about client-side (from a successful `admitPatient`/
 *    `transferPatient` response, keyed by bed id) and pre-fills the
 *    discharge/transfer form when that's available; for a bed that was
 *    already occupied before this session (or after a page reload), the
 *    field is blank and the caller must enter the admission UUID directly
 *    -- the same "no lookup, plain UUID text entry" precedent already
 *    established for `drug_id`/`patient_id` elsewhere in this codebase.
 *    A field hint says this explicitly rather than silently failing.
 *  - **Transfer target picker**: rather than a blind free-text bed-ID
 *    input, the "new bed" field is a `<select>` populated from the beds
 *    already loaded into this matrix with `status === "available"` --
 *    this data is already on-hand client-side (no extra lookup endpoint
 *    needed) and is meaningfully better UX than asking staff to copy a
 *    bed UUID by hand.
 */
export default function BedMatrixPage(): JSX.Element {
  const { user } = useAuth();
  const role = user?.role;
  const isSystemAdmin = role === "system_admin";
  const canAct = role === "nurse" || role === "doctor" || role === "system_admin";
  const canChangeStatus = role === "nurse" || role === "system_admin";

  const [branchId, setBranchId] = useState<string>("");
  const [wardIdFilter, setWardIdFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<BedStatus | "">("");

  const [beds, setBeds] = useState<BedMatrixEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  const [admissionIdByBed, setAdmissionIdByBed] = useState<Record<string, string>>({});

  const [openAction, setOpenAction] = useState<{ bedId: string; action: RowAction } | null>(null);
  const [admitForm, setAdmitForm] = useState<AdmitFormState>(emptyAdmitForm);
  const [dischargeForm, setDischargeForm] = useState<DischargeFormState>(emptyDischargeForm);
  const [transferForm, setTransferForm] = useState<TransferFormState>(emptyTransferForm);

  const [rowErrors, setRowErrors] = useState<Record<string, string | null>>({});
  const [pendingBedId, setPendingBedId] = useState<string | null>(null);

  const load = async (): Promise<void> => {
    if (isSystemAdmin && !branchId.trim()) {
      return;
    }
    setIsLoading(true);
    setLoadError(null);
    try {
      const found = await getBedMatrix({
        branchId: isSystemAdmin ? branchId.trim() : undefined,
        wardId: wardIdFilter.trim() || undefined,
        status: statusFilter || undefined,
      });
      setBeds(found);
    } catch (error) {
      setLoadError(extractErrorMessage(error, "Could not load the bed matrix."));
    } finally {
      setIsLoading(false);
    }
  };

  // A non-system_admin's branch is implicit -- auto-load on mount. A
  // system_admin has no default branch, so nothing auto-loads for them;
  // see the doc comment above (same pattern as PharmacyInventoryPage.tsx).
  useEffect(() => {
    if (!isSystemAdmin) {
      void load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSystemAdmin]);

  const closeRowAction = (): void => {
    setOpenAction(null);
    setAdmitForm(emptyAdmitForm);
    setDischargeForm(emptyDischargeForm);
    setTransferForm(emptyTransferForm);
  };

  const openRowAction = (bedId: string, action: RowAction): void => {
    setRowErrors((previous) => ({ ...previous, [bedId]: null }));
    setOpenAction({ bedId, action });
    if (action === "discharge") {
      setDischargeForm({ admissionId: admissionIdByBed[bedId] ?? "", dischargeTime: "" });
    } else if (action === "transfer") {
      setTransferForm({ admissionId: admissionIdByBed[bedId] ?? "", newBedId: "" });
    } else {
      setAdmitForm(emptyAdmitForm);
    }
  };

  const handleAdmitSubmit = async (bed: BedMatrixEntry, event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!admitForm.patientId.trim() || !admitForm.startTime) {
      setRowErrors((previous) => ({ ...previous, [bed.id]: "Patient ID and start time are required." }));
      return;
    }
    setPendingBedId(bed.id);
    try {
      const admission = await admitPatient({
        patientId: admitForm.patientId.trim(),
        bedId: bed.id,
        startTime: new Date(admitForm.startTime).toISOString(),
      });
      setAdmissionIdByBed((previous) => ({ ...previous, [bed.id]: admission.id }));
      setBeds((previous) =>
        (previous ?? []).map((item) => (item.id === bed.id ? { ...item, status: "occupied" } : item)),
      );
      closeRowAction();
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [bed.id]: extractErrorMessage(error, "Could not admit this patient."),
      }));
    } finally {
      setPendingBedId(null);
    }
  };

  const handleDischargeSubmit = async (
    bed: BedMatrixEntry,
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (!dischargeForm.admissionId.trim()) {
      setRowErrors((previous) => ({ ...previous, [bed.id]: "Admission ID is required." }));
      return;
    }
    setPendingBedId(bed.id);
    try {
      await dischargePatient(
        dischargeForm.admissionId.trim(),
        dischargeForm.dischargeTime ? new Date(dischargeForm.dischargeTime).toISOString() : undefined,
      );
      setAdmissionIdByBed((previous) => {
        const next = { ...previous };
        delete next[bed.id];
        return next;
      });
      setBeds((previous) =>
        (previous ?? []).map((item) => (item.id === bed.id ? { ...item, status: "cleaning" } : item)),
      );
      closeRowAction();
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [bed.id]: extractErrorMessage(error, "Could not discharge this patient."),
      }));
    } finally {
      setPendingBedId(null);
    }
  };

  const handleTransferSubmit = async (
    bed: BedMatrixEntry,
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (!transferForm.admissionId.trim() || !transferForm.newBedId) {
      setRowErrors((previous) => ({ ...previous, [bed.id]: "Admission ID and a target bed are required." }));
      return;
    }
    setPendingBedId(bed.id);
    try {
      const newAdmission = await transferPatient(transferForm.admissionId.trim(), transferForm.newBedId);
      setAdmissionIdByBed((previous) => {
        const next = { ...previous };
        delete next[bed.id];
        next[newAdmission.bedId] = newAdmission.id;
        return next;
      });
      setBeds((previous) =>
        (previous ?? []).map((item) => {
          if (item.id === bed.id) {
            return { ...item, status: "cleaning" };
          }
          if (item.id === newAdmission.bedId) {
            return { ...item, status: "occupied" };
          }
          return item;
        }),
      );
      closeRowAction();
    } catch (error) {
      setRowErrors((previous) => ({
        ...previous,
        [bed.id]: extractErrorMessage(error, "Could not transfer this patient."),
      }));
    } finally {
      setPendingBedId(null);
    }
  };

  const handleStatusChange = async (
    bed: BedMatrixEntry,
    requested: Extract<BedStatus, "available" | "blocked">,
  ): Promise<void> => {
    setRowErrors((previous) => ({ ...previous, [bed.id]: null }));
    setPendingBedId(bed.id);
    try {
      const updated = await updateBedStatus(bed.id, requested);
      setBeds((previous) =>
        (previous ?? []).map((item) => (item.id === bed.id ? { ...item, status: updated.status } : item)),
      );
    } catch (error) {
      if (error instanceof IllegalBedStatusTransitionError) {
        setRowErrors((previous) => ({
          ...previous,
          [bed.id]: `Cannot change this bed from "${STATUS_LABEL[error.current]}" to "${
            STATUS_LABEL[error.requested]
          }".`,
        }));
      } else {
        setRowErrors((previous) => ({
          ...previous,
          [bed.id]: extractErrorMessage(error, "Could not update this bed's status."),
        }));
      }
    } finally {
      setPendingBedId(null);
    }
  };

  const wardGroups = new Map<string, { wardName: string; beds: BedMatrixEntry[] }>();
  for (const bed of beds ?? []) {
    const group = wardGroups.get(bed.wardId);
    if (group) {
      group.beds.push(bed);
    } else {
      wardGroups.set(bed.wardId, { wardName: bed.wardName, beds: [bed] });
    }
  }

  const availableBedsForTransfer = (excludeBedId: string): BedMatrixEntry[] =>
    (beds ?? []).filter((item) => item.status === "available" && item.id !== excludeBedId);

  return (
    <main className="page-container">
      <h1>Bed matrix</h1>

      <div className="section-header">
        <h2>Filters</h2>
      </div>
      <div className="patient-form">
        {isSystemAdmin && (
          <>
            <label className="auth-label" htmlFor="bed-matrix-branch-id">
              Branch ID
            </label>
            <input
              id="bed-matrix-branch-id"
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
        <label className="auth-label" htmlFor="bed-matrix-ward-id">
          Ward ID (optional)
        </label>
        <input
          id="bed-matrix-ward-id"
          className="auth-input"
          type="text"
          placeholder="00000000-0000-0000-0000-000000000000"
          value={wardIdFilter}
          onChange={(event) => setWardIdFilter(event.target.value)}
        />
        <label className="auth-label" htmlFor="bed-matrix-status">
          Status
        </label>
        <select
          id="bed-matrix-status"
          className="auth-input"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as BedStatus | "")}
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

      {isSystemAdmin && beds === null && (
        <p className="empty-state">Enter a branch ID above, then click "Refresh" to view its bed matrix.</p>
      )}

      {beds !== null && beds.length === 0 && (
        <p className="empty-state">No beds match the current filters.</p>
      )}

      {beds !== null &&
        Array.from(wardGroups.entries()).map(([wardId, group]) => (
          <section key={wardId} className="ward-section">
            <h2>{group.wardName}</h2>
            <div className="bed-grid">
              {group.beds.map((bed) => {
                const rowError = rowErrors[bed.id] ?? null;
                const isPending = pendingBedId === bed.id;
                const isOpenFor = (action: RowAction): boolean =>
                  openAction?.bedId === bed.id && openAction.action === action;
                const manualTargets = legalManualTargets(bed.status);

                return (
                  <div key={bed.id} className="bed-card">
                    <div className="bed-card-header">
                      <h3>{bed.label}</h3>
                      <span className={STATUS_BADGE_CLASS[bed.status]}>{STATUS_LABEL[bed.status]}</span>
                    </div>

                    {rowError !== null && (
                      <p className="auth-error" role="alert">
                        {rowError}
                      </p>
                    )}

                    {canAct && (
                      <div className="bed-card-actions">
                        {bed.status === "available" && !isOpenFor("admit") && (
                          <button
                            className="button-secondary"
                            type="button"
                            disabled={isPending}
                            onClick={() => openRowAction(bed.id, "admit")}
                          >
                            Admit
                          </button>
                        )}
                        {bed.status === "occupied" && !isOpenFor("discharge") && !isOpenFor("transfer") && (
                          <>
                            <button
                              className="button-secondary"
                              type="button"
                              disabled={isPending}
                              onClick={() => openRowAction(bed.id, "discharge")}
                            >
                              Discharge
                            </button>
                            <button
                              className="button-secondary"
                              type="button"
                              disabled={isPending}
                              onClick={() => openRowAction(bed.id, "transfer")}
                            >
                              Transfer
                            </button>
                          </>
                        )}
                        {canChangeStatus &&
                          manualTargets.map((target) => (
                            <button
                              key={target}
                              className={target === "blocked" ? "button-danger" : "button-secondary"}
                              type="button"
                              disabled={isPending}
                              onClick={() => void handleStatusChange(bed, target)}
                            >
                              Mark {STATUS_LABEL[target].toLowerCase()}
                            </button>
                          ))}
                      </div>
                    )}

                    {isOpenFor("admit") && (
                      <form
                        className="override-reason-form"
                        onSubmit={(event) => void handleAdmitSubmit(bed, event)}
                      >
                        <label className="auth-label" htmlFor={`admit-patient-${bed.id}`}>
                          Patient ID (UUID)
                        </label>
                        <input
                          id={`admit-patient-${bed.id}`}
                          className="auth-input"
                          type="text"
                          value={admitForm.patientId}
                          onChange={(event) =>
                            setAdmitForm((previous) => ({ ...previous, patientId: event.target.value }))
                          }
                          required
                        />
                        <label className="auth-label" htmlFor={`admit-start-${bed.id}`}>
                          Start time
                        </label>
                        <input
                          id={`admit-start-${bed.id}`}
                          className="auth-input"
                          type="datetime-local"
                          value={admitForm.startTime}
                          onChange={(event) =>
                            setAdmitForm((previous) => ({ ...previous, startTime: event.target.value }))
                          }
                          required
                        />
                        <div className="conflict-actions">
                          <button className="auth-submit" type="submit" disabled={isPending}>
                            {isPending ? "Admitting..." : "Confirm admit"}
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

                    {isOpenFor("discharge") && (
                      <form
                        className="override-reason-form"
                        onSubmit={(event) => void handleDischargeSubmit(bed, event)}
                      >
                        <label className="auth-label" htmlFor={`discharge-admission-${bed.id}`}>
                          Admission ID (UUID)
                        </label>
                        <input
                          id={`discharge-admission-${bed.id}`}
                          className="auth-input"
                          type="text"
                          value={dischargeForm.admissionId}
                          onChange={(event) =>
                            setDischargeForm((previous) => ({ ...previous, admissionId: event.target.value }))
                          }
                          required
                        />
                        <p className="field-hint">
                          No endpoint looks up the active admission for a bed -- pre-filled if this bed was
                          admitted earlier in this browser session, otherwise enter it directly.
                        </p>
                        <label className="auth-label" htmlFor={`discharge-time-${bed.id}`}>
                          Discharge time (optional)
                        </label>
                        <input
                          id={`discharge-time-${bed.id}`}
                          className="auth-input"
                          type="datetime-local"
                          value={dischargeForm.dischargeTime}
                          onChange={(event) =>
                            setDischargeForm((previous) => ({
                              ...previous,
                              dischargeTime: event.target.value,
                            }))
                          }
                        />
                        <div className="conflict-actions">
                          <button className="auth-submit" type="submit" disabled={isPending}>
                            {isPending ? "Discharging..." : "Confirm discharge"}
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

                    {isOpenFor("transfer") && (
                      <form
                        className="override-reason-form"
                        onSubmit={(event) => void handleTransferSubmit(bed, event)}
                      >
                        <label className="auth-label" htmlFor={`transfer-admission-${bed.id}`}>
                          Admission ID (UUID)
                        </label>
                        <input
                          id={`transfer-admission-${bed.id}`}
                          className="auth-input"
                          type="text"
                          value={transferForm.admissionId}
                          onChange={(event) =>
                            setTransferForm((previous) => ({ ...previous, admissionId: event.target.value }))
                          }
                          required
                        />
                        <p className="field-hint">
                          No endpoint looks up the active admission for a bed -- pre-filled if this bed was
                          admitted earlier in this browser session, otherwise enter it directly.
                        </p>
                        <label className="auth-label" htmlFor={`transfer-bed-${bed.id}`}>
                          New bed
                        </label>
                        <select
                          id={`transfer-bed-${bed.id}`}
                          className="auth-input"
                          value={transferForm.newBedId}
                          onChange={(event) =>
                            setTransferForm((previous) => ({ ...previous, newBedId: event.target.value }))
                          }
                          required
                        >
                          <option value="">Select an available bed...</option>
                          {availableBedsForTransfer(bed.id).map((target) => (
                            <option key={target.id} value={target.id}>
                              {target.wardName} — {target.label}
                            </option>
                          ))}
                        </select>
                        <div className="conflict-actions">
                          <button className="auth-submit" type="submit" disabled={isPending}>
                            {isPending ? "Transferring..." : "Confirm transfer"}
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
                  </div>
                );
              })}
            </div>
          </section>
        ))}
    </main>
  );
}
