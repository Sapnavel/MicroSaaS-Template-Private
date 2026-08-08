import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import DrugSelect from "../components/selects/DrugSelect";
import {
  createPrescription,
  getConsultation,
  PrescriptionBlockedError,
  PrescriptionOverrideRequiredError,
  type PrescriptionItemInput,
} from "../services/consultationService";
import type { Consultation, PrescriptionCreateResult, SafetyFinding, SafetyTier } from "../types";
import { extractErrorMessage } from "../utils/errors";

interface PrescriptionItemFormRow {
  drugId: string;
  dosage: string;
  frequency: string;
  /** Kept as a string for the input; "" means "leave duration unset" (an
   * ongoing/chronic course), converted to `number | null` at submit time. */
  durationDays: string;
}

const emptyRow: PrescriptionItemFormRow = { drugId: "", dosage: "", frequency: "", durationDays: "" };

interface OverrideState {
  findings: SafetyFinding[];
  reason: string;
}

function tierClassName(tier: SafetyTier): string {
  return `safety-finding safety-finding--${tier.toLowerCase()}`;
}

function FindingList({ findings }: { findings: SafetyFinding[] }): JSX.Element {
  return (
    <ul className="safety-finding-list">
      {findings.map((finding, index) => (
        // Findings have no stable server-assigned id -- source+drugIds+index
        // is unique enough for this render-only list.
        <li key={`${finding.source}-${finding.drugIds.join(",")}-${index}`} className={tierClassName(finding.tier)}>
          <strong>
            [{finding.tier}] {finding.source}
          </strong>{" "}
          ({finding.severity}): {finding.message}
        </li>
      ))}
    </ul>
  );
}

/**
 * Prescription creation page for a consultation -- the safety-gated flow
 * from PRPs/clinical-consultation-prescription-prp.md "SAFETY ENGINE DESIGN"
 * and "ENDPOINTS". Three distinct outcomes are rendered distinctly on
 * purpose:
 *  - PrescriptionBlockedError (422, BLOCK tier): no override action is ever
 *    offered -- the only path forward is removing the offending drug(s) and
 *    resubmitting.
 *  - PrescriptionOverrideRequiredError (409, OVERRIDE_REQUIRED tier): an
 *    explicit "Override and proceed anyway" action, gated on a non-empty
 *    reason, mirroring PatientRegisterPage's conflict/force-create UX.
 *  - Success: the full safety report (including INFO-tier findings) is
 *    shown so the prescriber sees what was checked, not just silent success.
 *
 * Each row's `drugId` is a `<DrugSelect>` in place of the old raw-UUID text
 * box (frontend UUID-to-dropdown conversion follow-up).
 */
export default function PrescriptionPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();

  const [consultation, setConsultation] = useState<Consultation | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [rows, setRows] = useState<PrescriptionItemFormRow[]>([{ ...emptyRow }]);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);

  const [blockedFindings, setBlockedFindings] = useState<SafetyFinding[] | null>(null);
  const [overrideState, setOverrideState] = useState<OverrideState | null>(null);
  const [result, setResult] = useState<PrescriptionCreateResult | null>(null);

  useEffect(() => {
    if (!id) {
      return;
    }
    const load = async (): Promise<void> => {
      try {
        const found = await getConsultation(id);
        setConsultation(found);
      } catch (error) {
        setLoadError(extractErrorMessage(error, "Could not load this consultation."));
      }
    };
    void load();
  }, [id]);

  const updateRow = <K extends keyof PrescriptionItemFormRow>(
    index: number,
    field: K,
    value: PrescriptionItemFormRow[K],
  ): void => {
    setRows((previous) => previous.map((row, rowIndex) => (rowIndex === index ? { ...row, [field]: value } : row)));
  };

  const addRow = (): void => {
    setRows((previous) => [...previous, { ...emptyRow }]);
  };

  const removeRow = (index: number): void => {
    setRows((previous) => previous.filter((_, rowIndex) => rowIndex !== index));
  };

  const buildItems = (): PrescriptionItemInput[] | null => {
    if (rows.length === 0) {
      setFormError("Add at least one drug to prescribe.");
      return null;
    }
    const items: PrescriptionItemInput[] = [];
    for (const row of rows) {
      if (!row.drugId.trim() || !row.dosage.trim() || !row.frequency.trim()) {
        setFormError("Every drug row needs a drug ID, dosage, and frequency.");
        return null;
      }
      let durationDays: number | null = null;
      if (row.durationDays.trim() !== "") {
        const parsed = Number(row.durationDays);
        if (!Number.isInteger(parsed) || parsed <= 0) {
          setFormError("Duration (days) must be a positive whole number, or left blank for an ongoing course.");
          return null;
        }
        durationDays = parsed;
      }
      items.push({
        drugId: row.drugId.trim(),
        dosage: row.dosage.trim(),
        frequency: row.frequency.trim(),
        durationDays,
      });
    }
    return items;
  };

  const submit = async (override: boolean, overrideReason?: string): Promise<void> => {
    if (!id) {
      return;
    }
    const items = buildItems();
    if (!items) {
      return;
    }
    setIsSubmitting(true);
    try {
      const created = await createPrescription(id, items, override, overrideReason);
      setResult(created);
      setBlockedFindings(null);
      setOverrideState(null);
      setFormError(null);
    } catch (error) {
      if (error instanceof PrescriptionBlockedError) {
        setBlockedFindings(error.blockingFindings);
        setOverrideState(null);
        setFormError(null);
      } else if (error instanceof PrescriptionOverrideRequiredError) {
        setOverrideState({ findings: error.overrideRequiredFindings, reason: "" });
        setBlockedFindings(null);
        setFormError(null);
      } else {
        setFormError(extractErrorMessage(error, "Could not create this prescription."));
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setFormError(null);
    setBlockedFindings(null);
    setOverrideState(null);
    setResult(null);
    void submit(false);
  };

  const handleOverrideConfirm = (): void => {
    if (!overrideState || overrideState.reason.trim() === "") {
      return;
    }
    void submit(true, overrideState.reason.trim());
  };

  if (!id) {
    return (
      <main className="page-container">
        <p className="auth-error" role="alert">
          No consultation ID in the URL.
        </p>
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

  return (
    <main className="page-container">
      <h1>Prescription</h1>
      <p>
        <Link to={`/consultations/${id}`}>Back to consultation</Link>
      </p>
      {consultation && (
        <p className="field-hint">
          Patient ID: {consultation.patientId} &middot; Symptoms: {consultation.symptoms}
        </p>
      )}

      {result === null && (
        <form className="patient-form" onSubmit={handleSubmit}>
          <h3>Drugs to prescribe</h3>
          {formError !== null && (
            <p className="auth-error" role="alert">
              {formError}
            </p>
          )}

          {rows.map((row, index) => (
            <div className="prescription-item-row" key={index}>
              <div>
                <label className="auth-label" htmlFor={`drug-id-${index}`}>
                  Drug
                </label>
                <DrugSelect
                  id={`drug-id-${index}`}
                  value={row.drugId}
                  onChange={(value) => updateRow(index, "drugId", value)}
                />
              </div>
              <div>
                <label className="auth-label" htmlFor={`dosage-${index}`}>
                  Dosage
                </label>
                <input
                  id={`dosage-${index}`}
                  className="auth-input"
                  type="text"
                  placeholder="e.g. 500mg"
                  value={row.dosage}
                  onChange={(event) => updateRow(index, "dosage", event.target.value)}
                  required
                />
              </div>
              <div>
                <label className="auth-label" htmlFor={`frequency-${index}`}>
                  Frequency
                </label>
                <input
                  id={`frequency-${index}`}
                  className="auth-input"
                  type="text"
                  placeholder="e.g. twice daily"
                  value={row.frequency}
                  onChange={(event) => updateRow(index, "frequency", event.target.value)}
                  required
                />
              </div>
              <div>
                <label className="auth-label" htmlFor={`duration-${index}`}>
                  Duration (days)
                </label>
                <input
                  id={`duration-${index}`}
                  className="auth-input"
                  type="number"
                  min={1}
                  placeholder="ongoing"
                  value={row.durationDays}
                  onChange={(event) => updateRow(index, "durationDays", event.target.value)}
                />
              </div>
              <button
                className="button-danger"
                type="button"
                disabled={rows.length <= 1}
                onClick={() => removeRow(index)}
              >
                Remove
              </button>
            </div>
          ))}

          <p>
            <button className="button-secondary" type="button" onClick={addRow}>
              + Add another drug
            </button>
          </p>

          <button className="auth-submit" type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Checking safety..." : "Submit prescription"}
          </button>
        </form>
      )}

      {blockedFindings !== null && (
        <div className="status-banner status-banner--block" role="alert">
          <h3>Prescription blocked</h3>
          <p>
            One or more drugs in this request triggered a hard safety block. There is no override for this --
            remove the offending drug(s) below and resubmit.
          </p>
          <FindingList findings={blockedFindings} />
        </div>
      )}

      {overrideState !== null && (
        <div className="status-banner status-banner--override" role="alert">
          <h3>Safety review required</h3>
          <p>
            This request triggered findings that require an explicit acknowledgement before it can proceed.
          </p>
          <FindingList findings={overrideState.findings} />
          <div className="override-reason-form">
            <label className="auth-label" htmlFor="override-reason">
              Reason for override (required)
            </label>
            <textarea
              id="override-reason"
              className="textarea"
              rows={3}
              value={overrideState.reason}
              onChange={(event) =>
                setOverrideState((previous) => (previous ? { ...previous, reason: event.target.value } : previous))
              }
              placeholder="Explain why this is safe to proceed despite the findings above..."
              required
            />
            <div className="conflict-actions">
              <button
                className="button-danger"
                type="button"
                disabled={isSubmitting || overrideState.reason.trim() === ""}
                onClick={handleOverrideConfirm}
              >
                {isSubmitting ? "Submitting..." : "Override and proceed anyway"}
              </button>
            </div>
          </div>
        </div>
      )}

      {result !== null && (
        <div className="status-banner status-banner--success">
          <h3>Prescription created</h3>
          <p>
            Status: {result.prescription.status} &middot; {result.items.length} item(s)
          </p>
          <ul className="list-plain">
            {result.items.map((item) => (
              <li key={item.id} className="list-item-card">
                {item.drugId} — {item.dosage}, {item.frequency}
                {item.durationDays !== null ? `, ${item.durationDays} day(s)` : ", ongoing"}
              </li>
            ))}
          </ul>

          <h3>Safety report</h3>
          {result.safetyReport.allergyFindings.length === 0 && result.safetyReport.interactionFindings.length === 0 ? (
            <p className="empty-state">No allergy or interaction findings for this prescription.</p>
          ) : (
            <>
              {result.safetyReport.allergyFindings.length > 0 && (
                <>
                  <p>Allergy findings:</p>
                  <FindingList findings={result.safetyReport.allergyFindings} />
                </>
              )}
              {result.safetyReport.interactionFindings.length > 0 && (
                <>
                  <p>Interaction findings:</p>
                  <FindingList findings={result.safetyReport.interactionFindings} />
                </>
              )}
            </>
          )}

          <p>
            <button
              className="button-secondary"
              type="button"
              onClick={() => {
                setResult(null);
                setRows([{ ...emptyRow }]);
              }}
            >
              Prescribe another
            </button>
          </p>
        </div>
      )}
    </main>
  );
}
