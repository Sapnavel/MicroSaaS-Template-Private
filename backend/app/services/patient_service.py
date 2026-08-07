"""Patient Master Index business logic. Routers (routers/patients.py) stay
thin and delegate here -- same "business logic lives in services/, router is
thin" split as services/auth_service.py and services/scheduling_engine.py.

Design notes (see PRPs/patient-master-index-prp.md, MATCHING ENGINE DESIGN /
ENDPOINTS):
- `create_patient` runs the deterministic check
  (`patient_matching.find_deterministic_match`) synchronously before insert,
  then the probabilistic scan (`patient_matching.scan_for_duplicates`)
  synchronously right after commit -- see patient_matching's module
  docstring for why both are synchronous today (no background-job
  infrastructure exists yet for any module).
- `patients` has no `branch_id` (see patient_matching's module docstring /
  the PRP's "Key design decision") -- `authorize()`'s tenant guard is
  therefore a no-op here by design, not a gap. Role-based response shaping
  (`shape_patient_response`) is the real "front_desk vs. clinical roles"
  control in this module, not row-level tenant isolation.
- Every function that can fail in an API-visible way raises `HTTPException`
  directly (same convention as `auth_service.py`), except the
  deterministic-match 409 case: that needs a response body shape
  (`existing_patient_id`/`existing_patient_mrn` as top-level keys, not
  nested under FastAPI's default `{"detail": ...}` wrapping) that a plain
  `HTTPException(status_code=409, detail=...)` cannot produce, since
  FastAPI always wraps `detail` as `{"detail": <value>}`. `create_patient`
  therefore raises the module-local `DeterministicMatchConflict` instead,
  and `routers/patients.py` catches it and builds the exact response body
  by hand. This is a judgment call worth flagging: it's the one place this
  module deviates from "service layer raises HTTPException directly".
- Never let raw decrypted PHI (`phone`, `national_id`, `address`,
  `allergies_note`) reach a `logger.` call anywhere in this module --
  log lines below only ever reference ids/mrns/hashes.
"""

import logging
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.encryption import deterministic_hash
from app.core.security import authorize
from app.models.appointment import Appointment
from app.models.audit import record_audit_event
from app.models.patient import Patient, PatientDuplicateCandidate
from app.models.user import User, UserRole
from app.schemas.patient import (
    DuplicateCandidateResponse,
    PatientCreate,
    PatientDemographics,
    PatientFullRecord,
)
from app.services import patient_matching

logger = logging.getLogger(__name__)

# Search-before-create is human-driven "does this look right?" browsing, not
# an automated dedup decision -- deliberately looser than
# patient_matching.NAME_SIMILARITY_SCAN_THRESHOLD (0.4) used by the
# automated dedup scan, per the endpoint contract.
SEARCH_NAME_SIMILARITY_THRESHOLD = 0.3
SEARCH_RESULT_LIMIT = 50

# Registry of FKs to reassign from a merged-away ("loser") patient to the
# surviving record, per PRPs/patient-master-index-prp.md's merge workflow
# step 3. Currently exactly one entry: Appointment is the only implemented
# module that references patients today. Future module authors: append here
# -- e.g. (Consultation, "patient_id"), (LabOrder, "patient_id") -- as each
# module lands. Do not build speculative reassignment logic for tables that
# don't have ORM models yet.
PATIENT_FK_REASSIGNMENTS: list[tuple[type, str]] = [(Appointment, "patient_id")]


class DeterministicMatchConflict(Exception):
    """Raised by `create_patient` when `patient_matching.find_deterministic_match`
    hits and the caller did not pass `force=True`. Carries the existing
    patient so `routers/patients.py` can build the 409 response body's
    `existing_patient_id`/`existing_patient_mrn` fields -- see module
    docstring for why this isn't a plain HTTPException."""

    def __init__(self, existing_patient: Patient) -> None:
        self.existing_patient = existing_patient
        super().__init__(f"deterministic match found: existing patient {existing_patient.id}")


def _generate_mrn() -> str:
    """Generate a globally-unique Medical Record Number.

    No MRN generation scheme is specified anywhere in the PRP or
    schema.sql comments (`mrn TEXT NOT NULL UNIQUE` is the only
    constraint) -- this is a judgment call. Uses a random, non-sequential
    identifier (not an incrementing counter) deliberately: a sequential MRN
    leaks patient volume/ordering to anyone who can see two MRNs, and no
    sequence object exists in schema.sql to back one anyway. Collision
    probability is negligible (10 hex chars = 40 bits of entropy) and the
    `mrn` column's DB-level UNIQUE constraint is the actual backstop.
    """
    return f"MRN{uuid.uuid4().hex[:10].upper()}"


def _ordered_pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Deterministic ordering for a patient pair, so the same two patients
    always map to the same (patient_a_id, patient_b_id) tuple regardless of
    which one was "the new patient" in a given `create_patient` call. Backs
    the `UNIQUE (patient_a_id, patient_b_id)` constraint -- without this,
    creating patient X (which finds Y as a fuzzy candidate) and later
    creating some other patient that also finds Y and X as candidates could
    attempt to insert (Y, X) after (X, Y) already exists, violating the
    constraint."""
    return (a, b) if str(a) < str(b) else (b, a)


def _to_demographics(patient: Patient) -> PatientDemographics:
    return PatientDemographics.model_validate(patient)


def _to_full_record(patient: Patient) -> PatientFullRecord:
    return PatientFullRecord.model_validate(patient)


def shape_patient_response(user: User, patient: Patient) -> PatientDemographics | PatientFullRecord:
    """The single place a `Patient` ORM row becomes an API response, used by
    every endpoint that returns patient data (except the duplicates queue
    and merge response, which have their own fixed shapes per the endpoint
    contract -- see DuplicateCandidateResponse / MergeResponse docstrings).
    Calling this from one place only is deliberate: it's how "front_desk
    reads demographics only" stays enforced everywhere instead of being
    something a future endpoint author could forget.

    Calls `authorize(user, "patient", "read", patient)` first -- patients
    have no `branch_id` so this is a role-driven ABAC policy check, not a
    tenant guard (see module docstring). Raises 403 if no policy is
    registered for the caller's role (authorize() denies by default).
    """
    authorize(user, "patient", "read", patient)
    if user.role == UserRole.front_desk:
        return _to_demographics(patient)
    return _to_full_record(patient)


def _shape_duplicate_candidate(candidate: PatientDuplicateCandidate) -> DuplicateCandidateResponse:
    return DuplicateCandidateResponse(
        id=candidate.id,
        patient_a=_to_demographics(candidate.patient_a),
        patient_b=_to_demographics(candidate.patient_b),
        match_score=candidate.match_score,
        match_reason=candidate.match_reason,
        status=candidate.status,
        created_at=candidate.created_at,
    )


def create_patient(db: Session, current_user: User, payload: PatientCreate) -> Patient:
    """Create a patient, running the deterministic dedup check first (see
    `DeterministicMatchConflict`) and the probabilistic scan right after
    commit (see module docstring for why both run synchronously)."""
    # front_desk may create patients (and legitimately enters national_id/
    # address at intake, e.g. transcribing an insurance card) but can never
    # read allergies_note back (shape_patient_response strips it) and has no
    # plausible workflow for recording clinical allergy history -- unlike
    # national_id/address, letting front_desk write it was an unreviewed
    # asymmetry, not a considered call (REVIEW-AGENT finding M2). Reject
    # explicitly rather than silently dropping the submitted value, same
    # "never silently discard client input" principle as PatientCreate's
    # extra="forbid".
    if payload.allergies_note and current_user.role == UserRole.front_desk:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "front_desk cannot record clinical allergy information; a doctor or nurse must add allergies",
        )

    # Normalize before hashing: "555-123-4567" and "5551234567" are the same
    # number but must hash the same way for exact-match dedup to work at
    # all -- see patient_matching.normalize_phone_for_matching's docstring.
    phone_hash = deterministic_hash(patient_matching.normalize_phone_for_matching(payload.phone))
    national_id_hash = deterministic_hash(payload.national_id) if payload.national_id else None

    existing = patient_matching.find_deterministic_match(
        db,
        phone_hash=phone_hash,
        national_id_hash=national_id_hash,
        dob=payload.dob,
        full_name=payload.full_name,
    )
    if existing is not None and not payload.force:
        raise DeterministicMatchConflict(existing)

    # Retry on MRN collision (REVIEW-AGENT finding L1): _generate_mrn() is
    # random, not sequence-backed (see its docstring), so a collision -- rare
    # at 40 bits of entropy, but not impossible -- would otherwise surface as
    # an unhandled IntegrityError -> generic 500, indistinguishable to
    # front-desk staff from a real server fault. A few retries with a fresh
    # MRN turns that into a transparent success.
    patient: Patient | None = None
    for attempt in range(3):
        candidate_patient = Patient(
            mrn=_generate_mrn(),
            full_name=payload.full_name,
            dob=payload.dob,
            sex=payload.sex,
            phone=payload.phone,
            national_id=payload.national_id,
            address=payload.address,
            allergies_note=payload.allergies_note,
            phone_hash=phone_hash,
            national_id_hash=national_id_hash,
        )
        db.add(candidate_patient)
        try:
            # Flush (not commit) to get patient.id assigned -- needed as the
            # audit log's resource_id below when force=true, without
            # committing the new row until the audit entry is staged in the
            # same transaction.
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            if attempt == 2:
                raise
            logger.warning("MRN collision on attempt %d, regenerating and retrying: %s", attempt + 1, exc)
            continue
        patient = candidate_patient
        break
    assert patient is not None  # loop always either assigns patient or raises

    if existing is not None and payload.force:
        logger.info(
            "force-creating patient despite deterministic match: new=%s existing=%s",
            patient.id,
            existing.id,
        )
        # Whole-system review finding M2: metadata used to also include
        # `existing.mrn` -- a HIPAA Safe Harbor identifier -- duplicated
        # into the unencrypted `audit_logs.metadata` JSONB column. The MRN
        # is still returned in the 409 API response body by design (front
        # desk needs it to offer "use existing patient"), but that's a
        # different, already-reviewed tradeoff from writing it into the
        # audit trail too; `existing_patient_id` alone is enough for anyone
        # with legitimate audit access to look up the MRN via `patients.id`.
        record_audit_event(
            db,
            branch_id=None,  # patients are not branch-scoped, see module docstring
            actor_user_id=current_user.id,
            action="patient.force_create_despite_match",
            resource_type="patient",
            resource_id=str(patient.id),
            metadata={"existing_patient_id": str(existing.id)},
        )

    db.commit()
    db.refresh(patient)

    # Probabilistic pass: runs post-commit against the now-persisted patient
    # (scan_for_duplicates requires patient.id to exist -- see its docstring).
    _record_duplicate_candidates(db, patient)

    return patient


def _record_duplicate_candidates(db: Session, patient: Patient) -> None:
    """Insert a `patient_duplicate_candidates` row (status='pending') for
    each candidate `patient_matching.scan_for_duplicates` returns, unless a
    row already exists for that unordered pair -- checked regardless of the
    existing row's status (not just 'pending'), because the DB's
    `UNIQUE (patient_a_id, patient_b_id)` constraint doesn't care about
    status either: inserting a second row for a pair that was already
    reviewed (confirmed_merge/rejected) would still raise IntegrityError.
    """
    candidates = patient_matching.scan_for_duplicates(db, patient)
    for candidate_patient, score, reason in candidates:
        patient_a_id, patient_b_id = _ordered_pair(patient.id, candidate_patient.id)
        already_exists = db.execute(
            select(PatientDuplicateCandidate.id).where(
                PatientDuplicateCandidate.patient_a_id == patient_a_id,
                PatientDuplicateCandidate.patient_b_id == patient_b_id,
            )
        ).scalar_one_or_none()
        if already_exists is not None:
            continue
        db.add(
            PatientDuplicateCandidate(
                patient_a_id=patient_a_id,
                patient_b_id=patient_b_id,
                match_score=score,
                match_reason=reason,
                status="pending",
            )
        )
    if candidates:
        db.commit()


def search_patients(
    db: Session,
    *,
    name: str | None,
    dob: date | None,
    phone: str | None,
) -> list[Patient]:
    """GET /api/v1/patients/search. Caller (`routers/patients.py`) is
    responsible for the "at least one param required" 422 -- that's request
    validation, not a search-logic concern. Combines given params with AND.
    Excludes already-merged (tombstoned) patients: a front-desk search for
    "does this patient already exist" should surface the canonical survivor
    record, not a superseded one (judgment call -- not explicitly specified
    by the endpoint contract, but consistent with how
    `find_deterministic_match`/`scan_for_duplicates` both exclude
    `merged_into_id IS NOT NULL` rows).
    """
    conditions = [Patient.merged_into_id.is_(None)]
    similarity_expr = None
    if name:
        similarity_expr = func.similarity(Patient.full_name, name)
        conditions.append(similarity_expr > SEARCH_NAME_SIMILARITY_THRESHOLD)
    if dob:
        conditions.append(Patient.dob == dob)
    if phone:
        conditions.append(
            Patient.phone_hash == deterministic_hash(patient_matching.normalize_phone_for_matching(phone))
        )

    stmt = select(Patient).where(*conditions)
    if similarity_expr is not None:
        stmt = stmt.order_by(similarity_expr.desc())
    else:
        # No name given -> no similarity score to rank by. Order by
        # full_name for a stable, predictable result order (judgment call --
        # the contract only pins ordering for the name-given case).
        stmt = stmt.order_by(Patient.full_name)
    stmt = stmt.limit(SEARCH_RESULT_LIMIT)

    return list(db.execute(stmt).scalars().all())


def get_patient(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found")
    return patient


def list_pending_duplicates(db: Session) -> list[DuplicateCandidateResponse]:
    """GET /api/v1/patients/duplicates. Only `status == 'pending'` rows."""
    candidates = (
        db.execute(
            select(PatientDuplicateCandidate)
            # Eager-load both sides via joinedload -- without this, rendering
            # N pending rows into DuplicateCandidateResponse triggers 2N
            # extra lazy-load queries (one per patient_a/patient_b access).
            .options(joinedload(PatientDuplicateCandidate.patient_a), joinedload(PatientDuplicateCandidate.patient_b))
            .where(PatientDuplicateCandidate.status == "pending")
            .order_by(PatientDuplicateCandidate.created_at)
        )
        .unique()
        .scalars()
        .all()
    )
    return [_shape_duplicate_candidate(c) for c in candidates]


def merge_patients(
    db: Session,
    candidate_id: uuid.UUID,
    survivor_id: uuid.UUID,
    reviewed_by: uuid.UUID,
) -> tuple[Patient, int]:
    """Full merge_patients() transaction per the PRP's merge workflow:
    1. Load both patients FOR UPDATE (race guard against concurrent merges).
    2. Reject if either is already merged (no merge chains).
    3. Reassign FKs from loser to survivor (PATIENT_FK_REASSIGNMENTS).
    4. Set loser.merged_into_id.
    5. Mark the candidate row status='confirmed_merge', reviewed_by.
    6. Audit-log the merge.
    7. Commit once.

    Returns `(survivor, reassigned_appointments)`.
    """
    # FOR UPDATE, not db.get(): two near-simultaneous merge requests for the
    # SAME candidate_id must serialize on this row, or both could pass the
    # status=='pending' check below before either commits (REVIEW-AGENT C1).
    candidate = db.execute(
        select(PatientDuplicateCandidate).where(PatientDuplicateCandidate.id == candidate_id).with_for_update()
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "duplicate candidate not found")

    if candidate.status != "pending":
        # Blocks the exact failure REVIEW-AGENT C1 identified: a reviewer
        # already rejected this pair (or a concurrent request already merged
        # it) -- proceeding would silently merge two records a human already
        # judged as NOT the same person, which is the one thing this whole
        # module exists to prevent. This also covers a same-candidate replay
        # (double-submit, stale tab) after a merge already completed.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"this duplicate candidate is no longer pending review (status={candidate.status!r})",
        )

    if survivor_id not in (candidate.patient_a_id, candidate.patient_b_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "survivor_id must be either patient_a_id or patient_b_id of this candidate",
        )
    loser_id = candidate.patient_b_id if survivor_id == candidate.patient_a_id else candidate.patient_a_id

    # Lock both patient rows FOR UPDATE in a fixed (sorted-id) order, not the
    # survivor/loser order -- two concurrent merges racing on the same pair
    # (or on overlapping pairs) must acquire row locks in the same order to
    # avoid a Postgres deadlock, regardless of which side each transaction
    # calls "survivor".
    first_id, second_id = sorted((survivor_id, loser_id), key=str)
    first = db.execute(select(Patient).where(Patient.id == first_id).with_for_update()).scalar_one_or_none()
    second = db.execute(select(Patient).where(Patient.id == second_id).with_for_update()).scalar_one_or_none()
    if first is None or second is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "one or both patients in this candidate pair not found")

    survivor = first if first.id == survivor_id else second
    loser = first if first.id == loser_id else second

    if survivor.merged_into_id is not None or loser.merged_into_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "one or both patients in this candidate pair have already been merged",
        )

    # Chain guard (REVIEW-AGENT H1): the check above only catches the loser
    # itself already being tombstoned. It misses the mirror case -- the
    # loser already being the *survivor* of an earlier, separate merge (some
    # other patient's merged_into_id already points at it). Tombstoning it
    # now would leave that earlier patient's merged_into_id pointing at a
    # dead-end record instead of the true current canonical patient,
    # violating the "no merge chains" rule just as much as the case already
    # guarded against -- reachable through two ordinary, independently
    # legitimate review actions, no concurrency required. Reject and let a
    # human resolve it explicitly (re-review with the correct survivor)
    # rather than silently auto-repointing other patients' records as a
    # side effect of this merge.
    loser_has_followers = db.execute(
        select(Patient.id).where(Patient.merged_into_id == loser.id).limit(1)
    ).scalar_one_or_none()
    if loser_has_followers is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "the losing patient in this pair is already the canonical record for one or more "
            "previously-merged patients; merging it away would orphan those records. Resolve by "
            "merging survivor into this patient instead, or contact an administrator.",
        )

    reassignment_counts: dict[str, int] = {}
    for model_cls, fk_attr in PATIENT_FK_REASSIGNMENTS:
        result = db.execute(
            update(model_cls).where(getattr(model_cls, fk_attr) == loser.id).values(**{fk_attr: survivor.id})
        )
        reassignment_counts[model_cls.__name__] = result.rowcount
    reassigned_appointments = reassignment_counts.get("Appointment", 0)

    loser.merged_into_id = survivor.id
    db.add(loser)

    candidate.status = "confirmed_merge"
    candidate.reviewed_by = reviewed_by
    db.add(candidate)

    record_audit_event(
        db,
        branch_id=None,
        actor_user_id=reviewed_by,
        action="patient.merged",
        resource_type="patient",
        resource_id=str(survivor.id),
        metadata={
            "loser_id": str(loser.id),
            "candidate_id": str(candidate.id),
            "reassigned_appointments": reassigned_appointments,
            "reassignments": reassignment_counts,
        },
    )

    db.commit()
    db.refresh(survivor)
    return survivor, reassigned_appointments


def reject_duplicate_candidate(db: Session, candidate_id: uuid.UUID, reviewed_by: uuid.UUID) -> None:
    """Mark a candidate pair as not-a-duplicate. No FK changes, no audit log
    -- per the PRP, rejecting a false-positive match isn't the kind of
    security-relevant event the audit log is for."""
    candidate = db.get(PatientDuplicateCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "duplicate candidate not found")

    candidate.status = "rejected"
    candidate.reviewed_by = reviewed_by
    db.add(candidate)
    db.commit()
