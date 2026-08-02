import { useEffect, useState } from "react";

import { listDuplicates, mergeDuplicate, rejectDuplicate } from "../services/patientService";
import type { DuplicateCandidate, PatientDemographics } from "../types";
import { extractErrorMessage } from "../utils/errors";

/** "phone_hash" -> "Phone hash" for a human-readable match-signal label. */
function formatSignalLabel(key: string): string {
  const spaced = key.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

interface PatientColumnProps {
  patient: PatientDemographics;
  label: string;
}

function PatientColumn({ patient, label }: PatientColumnProps): JSX.Element {
  return (
    <div className="patient-card">
      <h3>
        {label}: {patient.fullName}
      </h3>
      <dl>
        <dt>MRN</dt>
        <dd>{patient.mrn}</dd>
        <dt>Date of birth</dt>
        <dd>{patient.dob}</dd>
        <dt>Phone</dt>
        <dd>{patient.phone}</dd>
      </dl>
    </div>
  );
}

export default function DuplicateReviewPage(): JSX.Element {
  const [candidates, setCandidates] = useState<DuplicateCandidate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyCandidateId, setBusyCandidateId] = useState<string | null>(null);

  useEffect(() => {
    const load = async (): Promise<void> => {
      try {
        const found = await listDuplicates();
        setCandidates(found);
      } catch (loadError) {
        setError(extractErrorMessage(loadError, "Could not load the duplicate review queue."));
      }
    };
    void load();
  }, []);

  const handleMerge = async (candidate: DuplicateCandidate, survivorId: string): Promise<void> => {
    setError(null);
    setBusyCandidateId(candidate.id);
    try {
      await mergeDuplicate(candidate.id, survivorId);
      setCandidates((previous) => (previous ?? []).filter((item) => item.id !== candidate.id));
    } catch (mergeError) {
      setError(extractErrorMessage(mergeError, "Merge failed. Please try again."));
    } finally {
      setBusyCandidateId(null);
    }
  };

  const handleReject = async (candidate: DuplicateCandidate): Promise<void> => {
    setError(null);
    setBusyCandidateId(candidate.id);
    try {
      await rejectDuplicate(candidate.id);
      setCandidates((previous) => (previous ?? []).filter((item) => item.id !== candidate.id));
    } catch (rejectError) {
      setError(extractErrorMessage(rejectError, "Could not mark this pair as not a duplicate."));
    } finally {
      setBusyCandidateId(null);
    }
  };

  return (
    <main className="page-container">
      <h1>Duplicate Patient Review</h1>

      {error !== null && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}

      {candidates === null ? (
        <p className="empty-state">Loading...</p>
      ) : candidates.length === 0 ? (
        <p className="empty-state">No pending duplicate candidates.</p>
      ) : (
        <ul className="duplicate-list">
          {candidates.map((candidate) => {
            const isBusy = busyCandidateId === candidate.id;
            const matchReasonEntries = Object.entries(candidate.matchReason);
            return (
              <li key={candidate.id} className="duplicate-card">
                <div className="duplicate-pair">
                  <PatientColumn patient={candidate.patientA} label="Patient A" />
                  <PatientColumn patient={candidate.patientB} label="Patient B" />
                </div>

                <div className="duplicate-meta">
                  <p>Match score: {candidate.matchScore.toFixed(2)}</p>
                  {matchReasonEntries.length > 0 && (
                    <>
                      <p>Signals that matched:</p>
                      <ul>
                        {matchReasonEntries.map(([signal, contribution]) => (
                          <li key={signal}>
                            {formatSignalLabel(signal)} (+{contribution.toFixed(2)})
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>

                <div className="duplicate-actions">
                  {/* Merge direction is an explicit choice per candidate --
                      one button per possible survivor -- rather than a
                      generic "Merge" button plus a follow-up prompt()
                      dialog, since the contract doesn't specify the exact
                      confirmation UX and a prompt() is harder to make
                      accessible/testable than two labeled buttons. */}
                  <button
                    className="button-secondary"
                    type="button"
                    disabled={isBusy}
                    onClick={() => void handleMerge(candidate, candidate.patientA.id)}
                  >
                    {isBusy ? "Working..." : "Merge (keep Patient A)"}
                  </button>
                  <button
                    className="button-secondary"
                    type="button"
                    disabled={isBusy}
                    onClick={() => void handleMerge(candidate, candidate.patientB.id)}
                  >
                    {isBusy ? "Working..." : "Merge (keep Patient B)"}
                  </button>
                  <button
                    className="button-danger"
                    type="button"
                    disabled={isBusy}
                    onClick={() => void handleReject(candidate)}
                  >
                    Not a duplicate
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
