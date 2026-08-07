import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import PatientSearchSelect from "../components/selects/PatientSearchSelect";
import WardSelect from "../components/selects/WardSelect";
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
  dischargeTime: string;
}

const emptyDischargeForm: DischargeFormState = { dischargeTime: "" };

interface TransferFormState {
  newBedId: string;
}

const emptyTransferForm: TransferFormState = { newBedId: "" };

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
 *  - `branchId` UX for `system_admin` is a `<BranchSelect>` (frontend
 *    UUID-to-dropdown conversion follow-up); the matrix doesn't auto-load
 *    until one is picked. Non-`system_admin` callers never see this field --
 *    their branch is implicit, so `branch_id` is simply never sent.
 *  - **Admission id** now comes straight off `BedMatrixEntry.activeAdmissionId`
 *    (the backend's `GET /wards/beds` extension, same conversion follow-up)
 *    instead of the old client-side "remember it from the last admit/transfer
 *    response, otherwise ask the user to type it in" workaround -- discharge
 *    and transfer read it directly from the occupied bed's own row, so
 *    there's no longer a manual admission-ID field on either form at all.
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
  // WardSelect needs a concrete branch id regardless of role -- a
  // non-system_admin's branch is implicit (never shown/edited as its own
  // field), so this falls back to their own `user.branchId`.
  const effectiveBranchId = isSystemAdmin ? branchId : user?.branchId ?? "";
  const [wardIdFilter, setWardIdFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<BedStatus | "">("");

  const [beds, setBeds] = useState<BedMatrixEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

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
      setDischargeForm(emptyDischargeForm);
    } else if (action === "transfer") {
      setTransferForm(emptyTransferForm);
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
      setBeds((previous) =>
        (previous ?? []).map((item) =>
          item.id === bed.id ? { ...item, status: "occupied", activeAdmissionId: admission.id } : item,
        ),
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
    if (!bed.activeAdmissionId) {
      setRowErrors((previous) => ({ ...previous, [bed.id]: "This bed has no open admission." }));
      return;
    }
    setPendingBedId(bed.id);
    try {
      await dischargePatient(
        bed.activeAdmissionId,
        dischargeForm.dischargeTime ? new Date(dischargeForm.dischargeTime).toISOString() : undefined,
      );
      setBeds((previous) =>
        (previous ?? []).map((item) =>
          item.id === bed.id ? { ...item, status: "cleaning", activeAdmissionId: null } : item,
        ),
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
    if (!bed.activeAdmissionId) {
      setRowErrors((previous) => ({ ...previous, [bed.id]: "This bed has no open admission." }));
      return;
    }
    if (!transferForm.newBedId) {
      setRowErrors((previous) => ({ ...previous, [bed.id]: "A target bed is required." }));
      return;
    }
    setPendingBedId(bed.id);
    try {
      const newAdmission = await transferPatient(bed.activeAdmissionId, transferForm.newBedId);
      setBeds((previous) =>
        (previous ?? []).map((item) => {
          if (item.id === bed.id) {
            return { ...item, status: "cleaning", activeAdmissionId: null };
          }
          if (item.id === newAdmission.bedId) {
            return { ...item, status: "occupied", activeAdmissionId: newAdmission.id };
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
              Branch
            </label>
            <BranchSelect id="bed-matrix-branch-id" value={branchId} onChange={setBranchId} />
          </>
        )}
        <label className="auth-label" htmlFor="bed-matrix-ward-id">
          Ward (optional)
        </label>
        <WardSelect
          id="bed-matrix-ward-id"
          value={wardIdFilter}
          onChange={setWardIdFilter}
          branchId={effectiveBranchId}
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
        <p className="empty-state">Select a branch above, then click "Refresh" to view its bed matrix.</p>
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
                          Patient
                        </label>
                        <PatientSearchSelect
                          id={`admit-patient-${bed.id}`}
                          value={admitForm.patientId}
                          onChange={(value) => setAdmitForm((previous) => ({ ...previous, patientId: value }))}
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
