"""Patient identity matching / deduplication engine.

Mirrors the rigor of `services/scheduling_engine.py` (tri-resource booking
was that module's "hard part"; identity resolution is this module's — see
PRPs/patient-master-index-prp.md, "MATCHING ENGINE DESIGN"). Two independent
passes, deliberately different in shape:

  1. Deterministic pass (`find_deterministic_match`) — exact-match only,
     synchronous, blocks patient creation. Cheap and unambiguous: same
     phone_hash, same national_id_hash, or same DOB + normalized name is
     treated as "this is almost certainly the same human," so the caller
     (Phase 2's `patient_service.create_patient`) is expected to refuse the
     create with a 409 unless staff explicitly overrides with `force=true`.
  2. Probabilistic pass (`scan_for_duplicates`) — fuzzy, runs *after* a
     patient already exists, and never blocks anything. It only ever
     produces candidate rows for human review.

Why auto-merge is never triggered by either pass, at any score, however
high: a wrong merge silently corrupts two different people's medical
histories into one record -- allergies, diagnoses, prescriptions,
lab results all become misattributed, and there is no clean way to undo
it after downstream clinical modules have written against the merged
record. `patient_duplicate_candidates` rows are therefore always
`status='pending'` until a human staff member calls the review endpoints
(Phase 2) -- `merge_patients` / `reject_duplicate_candidate` -- which this
module does NOT implement (see boundary note on `scan_for_duplicates`
below). The matching engine's job stops at "here is a scored, explained
list of candidates"; the merge decision itself is always a person's call.

Why synchronous, not event-driven, for now: docs/ARCHITECTURE.md's RabbitMQ
event bus exists for exactly this kind of decoupled background work, but no
consumer infrastructure exists yet for *any* module in this codebase (the
Auth module didn't wire one either). Running the probabilistic scan inline,
right after a successful create, is the pragmatic choice today. The seam is
deliberately kept to a single function call --
`patient_matching.scan_for_duplicates(db, patient)` -- so a future PRP can
move this behind `event_publisher.publish("patient.created", ...)` and a
consumer without touching the create-patient endpoint at all.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.patient import Patient

# --- MATCHING ENGINE DESIGN weights (PRPs/patient-master-index-prp.md) -----
# Kept as module-level constants (not magic numbers inline) so the weight
# table in the PRP and the code can be diffed against each other directly.
PHONE_HASH_WEIGHT = 0.40
NATIONAL_ID_HASH_WEIGHT = 0.45
DOB_WEIGHT = 0.15
FULL_NAME_TRIGRAM_WEIGHT = 0.25

# Probabilistic-pass thresholds.
NAME_SIMILARITY_SCAN_THRESHOLD = 0.4  # Postgres pg_trgm similarity() floor for the candidate scan itself

# NOTE: the PRP's draft weight table specified 0.5 here. Corrected to 0.25
# during Phase 1 validation: DOB_WEIGHT (0.15) + FULL_NAME_TRIGRAM_WEIGHT
# (0.25) tops out at 0.40 even at perfect name similarity, so a 0.5 floor
# would make the "DOB matches, name is a typo" case -- the exact scenario
# this scan exists to catch, per the module overview -- mathematically
# unreachable in the normal (non-force-duplicate) flow: it would only ever
# fire when phone_hash or national_id_hash ALSO match, which the
# deterministic pass already intercepts before a duplicate patient is even
# created. 0.25 is the floor of what a SQL-prefilter-qualifying row can
# score (dob exact + similarity just above the 0.4 scan threshold =
# 0.15 + 0.4*0.25 = 0.25), so this threshold effectively surfaces every row
# that passed the SQL prefilter, using the score for review-queue ranking
# (a phone/national_id match on top pushes a candidate toward 1.0) rather
# than as a second gate that silently discards everything the first gate let through.
DUPLICATE_CANDIDATE_SCORE_THRESHOLD = 0.25


@dataclass(frozen=True)
class MatchFields:
    """The subset of patient fields the matching engine reasons about.

    Deliberately not a full `Patient` ORM row: the deterministic check runs
    at patient-creation time, before a row necessarily exists yet, and the
    scoring function is pure Python with no DB dependency (see module
    docstring's "why synchronous" note doesn't apply here -- this is a
    separate design choice: scoring shouldn't require an ORM object just to
    be unit-testable). `phone_hash` / `national_id_hash` are the deterministic
    hash columns (app.core.encryption.deterministic_hash output), never raw
    PHI -- this module never sees decrypted phone numbers or national IDs.
    """

    phone_hash: str | None = None
    national_id_hash: str | None = None
    dob: date | None = None
    full_name: str | None = None


def normalize_phone_for_matching(phone: str) -> str:
    """Digits only, for hashing before `deterministic_hash()`.

    `deterministic_hash()` (app.core.encryption) only does `.strip().lower()`
    -- fine for values that are already canonical, but phone numbers rarely
    are: "555-123-4567", "(555) 123-4567", and "5551234567" are the same
    number but would hash to three different values without this step,
    silently defeating the highest-weighted dedup signal (PHONE_HASH_WEIGHT,
    0.40). Every caller that computes or compares a `phone_hash` -- both
    `patient_service.create_patient` and `patient_service.search_patients`
    -- must normalize with this function before calling `deterministic_hash`,
    or the two will disagree.

    Deliberately not extended to `national_id`: ID formats vary too widely
    across countries (Aadhaar groups digits with spaces, SSN uses dashes,
    others mix letters and digits meaningfully) for one digits-only rule to
    be correct everywhere -- a per-country normalizer is a real feature, not
    a one-line fix, and out of scope for this PRP. National ID matching
    today requires the caller to have entered it identically to how it was
    stored, which is a known, narrower limitation than the phone case (kept
    as-is rather than silently "fixed" with a wrong assumption).
    """
    return "".join(ch for ch in phone if ch.isdigit())


def normalize_name(full_name: str) -> str:
    """Lowercase, strip, collapse internal whitespace.

    Used for the deterministic "DOB + normalized full_name" equality check.
    Postgres can't cheaply express "collapse repeated internal whitespace"
    as an index-friendly expression, so this normalization is done in
    Python against a small same-DOB candidate set rather than pushed into
    SQL (see `find_deterministic_match`).
    """
    return " ".join(full_name.strip().lower().split())


def score_candidate(
    subject: MatchFields,
    candidate: MatchFields,
    name_similarity: float,
) -> tuple[float, dict[str, Any]]:
    """Pure weighted-signal scoring function -- no DB access.

    `name_similarity` is a precomputed trigram similarity in [0, 1] for the
    two patients' full_name values. Trigram similarity is a Postgres
    function (`similarity()`, from the `pg_trgm` extension); this function
    deliberately does not reimplement it in Python -- the caller
    (`scan_for_duplicates`) is expected to have already asked Postgres for
    the value via `func.similarity(...)` and pass it in here. That keeps
    this function trivially unit-testable with no DB fixture, per the PRP's
    Validation Gate 1 ("score function is pure Python").

    Returns `(score, match_reason)` where `match_reason` is a JSON-safe
    dict recording exactly which signals fired and their individual
    contribution -- this becomes the `patient_duplicate_candidates.match_reason`
    JSONB value in Phase 2's service layer, so reviewers can see *why* a
    pair matched, not just the aggregate number.
    """
    reasons: dict[str, Any] = {}
    score = 0.0

    if subject.phone_hash is not None and candidate.phone_hash is not None and subject.phone_hash == candidate.phone_hash:
        score += PHONE_HASH_WEIGHT
        reasons["phone_hash"] = {"matched": True, "weight": PHONE_HASH_WEIGHT}

    # Weight only applies "if both non-null" per the PRP -- a national ID
    # is optional, and two patients both missing one tells you nothing.
    if (
        subject.national_id_hash is not None
        and candidate.national_id_hash is not None
        and subject.national_id_hash == candidate.national_id_hash
    ):
        score += NATIONAL_ID_HASH_WEIGHT
        reasons["national_id_hash"] = {"matched": True, "weight": NATIONAL_ID_HASH_WEIGHT}

    if subject.dob is not None and candidate.dob is not None and subject.dob == candidate.dob:
        score += DOB_WEIGHT
        reasons["dob"] = {"matched": True, "weight": DOB_WEIGHT}

    if name_similarity > 0:
        name_contribution = round(name_similarity * FULL_NAME_TRIGRAM_WEIGHT, 4)
        score += name_contribution
        reasons["full_name"] = {
            "similarity": round(name_similarity, 4),
            "weight_applied": name_contribution,
        }

    score = min(score, 1.0)
    reasons["total_score"] = round(score, 4)
    return score, reasons


def find_deterministic_match(
    db: Session,
    *,
    phone_hash: str | None,
    national_id_hash: str | None,
    dob: date,
    full_name: str,
    exclude_patient_id: uuid.UUID | None = None,
) -> Patient | None:
    """Deterministic (exact-match) dedup check, run synchronously at
    patient-creation time.

    Matches an existing, non-tombstoned (`merged_into_id IS NULL`) patient
    on ANY of:
      - `phone_hash` equality
      - `national_id_hash` equality (only meaningful when both sides are
        non-null; the caller passes `national_id_hash=None` when the new
        patient has none, which correctly excludes this branch)
      - `dob` equality AND normalized `full_name` equality

    Returns the first matching `Patient`, or `None` if no exact match
    exists. Callers (Phase 2's `patient_service.create_patient`) are
    expected to treat a non-`None` return as grounds for a 409 Conflict
    unless the caller explicitly passed `force=true`, per the PRP.

    `exclude_patient_id` lets a caller re-run this check against a patient
    that already has an id (e.g. re-validating after an update) without
    matching itself; unused at initial-creation time since the new patient
    has no id yet.
    """
    normalized_name = normalize_name(full_name)

    # Single query fetches the union of all three candidate pools; the
    # candidate set is bounded (phone_hash/national_id_hash are unique-ish
    # and indexed, dob is indexed via idx_patients_dob_name) so this stays
    # cheap even though the final full_name-equality check happens in
    # Python rather than SQL.
    or_conditions = [Patient.dob == dob]
    if phone_hash:
        or_conditions.append(Patient.phone_hash == phone_hash)
    if national_id_hash:
        or_conditions.append(Patient.national_id_hash == national_id_hash)

    stmt = select(Patient).where(Patient.merged_into_id.is_(None), or_(*or_conditions))
    if exclude_patient_id is not None:
        stmt = stmt.where(Patient.id != exclude_patient_id)

    for row in db.execute(stmt).scalars().all():
        if phone_hash and row.phone_hash == phone_hash:
            return row
        if national_id_hash and row.national_id_hash == national_id_hash:
            return row
        if row.dob == dob and normalize_name(row.full_name) == normalized_name:
            return row
    return None


def scan_for_duplicates(db: Session, patient: Patient) -> list[tuple[Patient, float, dict[str, Any]]]:
    """Probabilistic dedup scan, run synchronously right after a successful
    patient create (see module docstring's "why synchronous" note).

    Queries existing patients (excluding `patient` itself, excluding
    already-tombstoned `merged_into_id IS NOT NULL` rows) where `dob`
    matches exactly and `similarity(full_name, patient.full_name) > 0.4`
    (Postgres `pg_trgm`, backed by `idx_patients_full_name_trgm`). Each hit
    is scored with `score_candidate`, using the exact similarity value
    Postgres computed as the `name_similarity` input -- this function does
    not recompute or approximate trigram similarity in Python.

    Requires `patient` to already be persisted (has an `.id`) -- this scans
    for OTHER patients that look like it, so it must exclude itself by id.

    Deliberate boundary: this function only returns scored candidates. It
    does NOT insert into `patient_duplicate_candidates` -- that write
    (including the `status='pending'` row-per-pair, respecting the
    `UNIQUE (patient_a_id, patient_b_id)` constraint) is Phase 2's
    `patient_service.create_patient` responsibility, since deciding *when*
    and *how* to persist a candidate (e.g. ordering patient_a/patient_b,
    skipping a pair that already has a row) is service-layer policy, not
    matching-engine logic. This split keeps the engine pure-ish and
    trivially testable against a real Postgres without needing to also
    exercise the insert/upsert path.
    """
    name_similarity_expr = func.similarity(Patient.full_name, patient.full_name)

    stmt = select(Patient, name_similarity_expr).where(
        Patient.id != patient.id,
        Patient.merged_into_id.is_(None),
        Patient.dob == patient.dob,
        name_similarity_expr > NAME_SIMILARITY_SCAN_THRESHOLD,
    )

    subject_fields = MatchFields(
        phone_hash=patient.phone_hash,
        national_id_hash=patient.national_id_hash,
        dob=patient.dob,
        full_name=patient.full_name,
    )

    results: list[tuple[Patient, float, dict[str, Any]]] = []
    for candidate, name_similarity in db.execute(stmt).all():
        candidate_fields = MatchFields(
            phone_hash=candidate.phone_hash,
            national_id_hash=candidate.national_id_hash,
            dob=candidate.dob,
            full_name=candidate.full_name,
        )
        score, reason = score_candidate(subject_fields, candidate_fields, float(name_similarity))
        if score >= DUPLICATE_CANDIDATE_SCORE_THRESHOLD:
            results.append((candidate, score, reason))
    return results
