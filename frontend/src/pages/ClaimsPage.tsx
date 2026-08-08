import { useState } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import ClaimSelect from "../components/selects/ClaimSelect";
import { useAuth } from "../hooks/useAuth";
import { setClaimState } from "../services/billingService";
import type { ClaimState, InsuranceClaim } from "../types";
import { extractErrorMessage } from "../utils/errors";

const CLAIM_STATE_OPTIONS: ClaimState[] = ["submitted", "adjudicating", "approved", "denied", "paid"];

const CLAIM_STATE_LABEL: Record<ClaimState, string> = {
  submitted: "Submitted",
  adjudicating: "Adjudicating",
  approved: "Approved",
  denied: "Denied",
  paid: "Paid",
};

const CLAIM_STATE_BADGE_CLASS: Record<ClaimState, string> = {
  submitted: "badge badge-primary",
  adjudicating: "badge badge-warning",
  approved: "badge badge-primary",
  denied: "badge badge-danger",
  paid: "badge badge-success",
};

/**
 * `_LEGAL_CLAIM_TRANSITIONS` mirrored client-side from
 * PRPs/billing-module-prp.md "ENGINE DESIGN" -- `denied`/`paid` are
 * terminal (never a source, empty array below).
 */
const LEGAL_NEXT_STATES: Record<ClaimState, ClaimState[]> = {
  submitted: ["adjudicating", "denied"],
  adjudicating: ["approved", "denied"],
  approved: ["paid"],
  denied: [],
  paid: [],
};

/**
 * Insurance claim adjudication page -- `billing_admin`/`system_admin` only
 * (no `front_desk` route registered for this page in `App.tsx`; claims
 * adjudication isn't a front-desk concern per the PRP's ENDPOINTS table).
 *
 * **Claim lookup** (frontend UUID-to-dropdown conversion follow-up):
 * `GET /api/v1/billing/claims?branch_id=&state=` now backs a `<ClaimSelect>`
 * here in place of the old raw-UUID text box. `branch_id` is a *required*
 * query param at the router for every role, including `billing_admin` (see
 * `billing_service.list_claims`'s docstring), but a `billing_admin`'s value
 * is always their own assigned branch, so this page only ever shows a
 * `<BranchSelect>` picker to `system_admin` -- same "implicit branch for
 * non-admin, explicit picker for system_admin" pattern as `BedMatrixPage.tsx`
 * -- and derives `effectiveBranchId` from `user.branchId` otherwise.
 *
 * Even with a proper lookup now, this page still can't know a claim's
 * *current* state before selecting it (the list endpoint's `state` column IS
 * shown per-option in the dropdown label, but there's no separate
 * `GET /claims/{id}` for a fresh read right before acting) -- the "Current
 * state" selector below remains the caller's own confirmation of what the
 * dropdown just told them, defaulting to `submitted` only when nothing is
 * selected yet. Once a transition succeeds, the selector snaps to the
 * backend's authoritative returned `state` and the full claim
 * (payer/claim amount/copay/updated_at) is shown below -- from then on the
 * displayed state is fact, not a guess. The backend's 409 on an illegal
 * transition remains the actual guard either way (defense in depth, not
 * just the button set) -- shown via `extractErrorMessage`.
 */
export default function ClaimsPage(): JSX.Element {
  const { user } = useAuth();
  const isSystemAdmin = user?.role === "system_admin";
  const [branchId, setBranchId] = useState<string>("");
  const effectiveBranchId = isSystemAdmin ? branchId : user?.branchId ?? "";
  const [claimId, setClaimId] = useState<string>("");
  const [currentState, setCurrentState] = useState<ClaimState>("submitted");
  const [hasConfirmedState, setHasConfirmedState] = useState<boolean>(false);
  const [lastResult, setLastResult] = useState<InsuranceClaim | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isPending, setIsPending] = useState<boolean>(false);

  const canAct = claimId.trim().length > 0;

  const handleTransition = async (requested: ClaimState): Promise<void> => {
    if (!canAct) {
      return;
    }
    setIsPending(true);
    setActionError(null);
    try {
      const updated = await setClaimState(claimId.trim(), requested);
      setCurrentState(updated.state);
      setHasConfirmedState(true);
      setLastResult(updated);
    } catch (error) {
      setActionError(extractErrorMessage(error, "Could not update this claim's state."));
    } finally {
      setIsPending(false);
    }
  };

  const handleClaimIdChange = (value: string): void => {
    setClaimId(value);
    setHasConfirmedState(false);
    setLastResult(null);
    setActionError(null);
  };

  const legalNextStates = LEGAL_NEXT_STATES[currentState];

  return (
    <main className="page-container">
      <h1>Insurance claims</h1>

      <div className="section-header">
        <h2>Look up a claim</h2>
      </div>
      <div className="patient-form">
        {isSystemAdmin && (
          <>
            <label className="auth-label" htmlFor="claim-branch-id">
              Branch
            </label>
            <BranchSelect id="claim-branch-id" value={branchId} onChange={setBranchId} />
          </>
        )}

        <label className="auth-label" htmlFor="claim-id">
          Claim
        </label>
        <ClaimSelect
          id="claim-id"
          value={claimId}
          onChange={handleClaimIdChange}
          branchId={effectiveBranchId}
        />

        <label className="auth-label" htmlFor="claim-current-state">
          Current state
        </label>
        <select
          id="claim-current-state"
          className="auth-input"
          value={currentState}
          disabled={hasConfirmedState}
          onChange={(event) => setCurrentState(event.target.value as ClaimState)}
        >
          {CLAIM_STATE_OPTIONS.map((state) => (
            <option key={state} value={state}>
              {CLAIM_STATE_LABEL[state]}
            </option>
          ))}
        </select>
        <p className="field-hint">
          {hasConfirmedState
            ? "Confirmed by the server's last response."
            : "There is no way to read a claim's state before the first transition -- set this to what you believe it currently is."}
        </p>
      </div>

      {actionError !== null && (
        <p className="auth-error" role="alert">
          {actionError}
        </p>
      )}

      <div className="section-header">
        <h2>
          Status: <span className={CLAIM_STATE_BADGE_CLASS[currentState]}>{CLAIM_STATE_LABEL[currentState]}</span>
        </h2>
      </div>

      {legalNextStates.length === 0 ? (
        <p className="empty-state">This state is terminal -- no further transitions are possible.</p>
      ) : (
        <div className="conflict-actions">
          {legalNextStates.map((target) => (
            <button
              key={target}
              className={target === "denied" ? "button-danger" : "button-secondary"}
              type="button"
              disabled={!canAct || isPending}
              onClick={() => void handleTransition(target)}
            >
              {isPending ? "Updating..." : `Move to ${CLAIM_STATE_LABEL[target].toLowerCase()}`}
            </button>
          ))}
        </div>
      )}

      {lastResult && (
        <>
          <div className="section-header">
            <h2>Claim details</h2>
          </div>
          <div className="list-item-card">
            <p>Payer: {lastResult.payerName}</p>
            <p>Claim amount: {lastResult.claimAmount.toFixed(2)}</p>
            <p>Patient copay: {lastResult.patientCopay.toFixed(2)}</p>
            <p>Last updated: {new Date(lastResult.updatedAt).toLocaleString()}</p>
          </div>
        </>
      )}
    </main>
  );
}
