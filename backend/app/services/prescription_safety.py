"""Drug interaction + allergy safety engine for the Smart Prescription
Engine (PRPs/clinical-consultation-prescription-prp.md, "SAFETY ENGINE
DESIGN"). Mirrors the rigor and shape of `services/patient_matching.py`:
pure, DB-independent scoring/classification functions kept separate from
the functions that query Postgres, so the classification table itself is
unit-testable with no DB fixture (this PRP's Validation Gate 1 calls that
out explicitly).

SCOPE LIMITATION -- read before trusting this module's output clinically:
The seeded `drugs`/`drug_interactions` data (see
`database/seed_clinical_reference_data.sql`) is a small illustrative
scaffold -- roughly a dozen well-known drugs and a handful of textbook
interaction pairs (e.g. Warfarin x Aspirin). It is NOT a real, clinically
complete or validated drug/interaction database. A real deployment must
integrate a licensed reference source (RxNorm, First Databank, DrugBank, or
similar) behind the same `check_drug_interactions`/`check_allergy_conflicts`
seam this module defines -- swapping the reference data is meant to be
possible without changing the classification logic below.

ALLERGY MATCHING -- a documented, deliberate limitation, not a bug:
`patient_allergies.substance` is clinician-entered free text ("penicillin",
"NSAIDs", ...); there is no allergen ontology in this schema and building
one is out of scope. `check_allergy_conflicts` matches a candidate drug
against a patient's allergies by exact, case-insensitive string equality
against ANY of `drug.name`, `drug.generic_name`, `drug.interaction_class`.
This has a known false-negative gap: a patient recorded as allergic to
"beta-lactams" as a class will NOT match a drug named "Amoxicillin" unless
`drug.interaction_class` happens to be exactly the string "beta-lactams"
too. This is intentional -- do not "fix" it with fuzzy/substring matching.
An approximate allergy match that's wrong in either direction (a false
clear that misses a real allergy, or a false alarm that blocks a safe drug)
is worse than an exact match with a documented, visible gap. If this gap
matters for a given deployment, the fix is a real allergen ontology /
class-mapping table, not fuzzy string matching bolted onto this function.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.consultation import Drug, DrugInteraction, PatientAllergy, Prescription, PrescriptionItem

# "Active" prescription statuses for the interaction check -- draft
# prescriptions were never finalized/dispensed and don't represent a real
# concurrent medication, per the PRP's "SAFETY ENGINE DESIGN" section.
_ACTIVE_PRESCRIPTION_STATUSES = ("finalized", "dispensed")


class SafetyTier(str, Enum):
    """The three-tier outcome from the PRP's classification table.

    `str` mixin (same pattern as `AppointmentStatus`) so a tier serializes
    as its plain string value in API responses / audit-log metadata without
    a custom encoder.
    """

    BLOCK = "BLOCK"
    OVERRIDE_REQUIRED = "OVERRIDE_REQUIRED"
    INFO = "INFO"


# --- Tier-classification tables (PRP "Three-tier classification") ---------
# Kept as module-level dicts, not inline if/elif chains, so the PRP's table
# and the code can be diffed against each other directly (same rationale as
# patient_matching.py's weight constants).
_ALLERGY_SEVERITY_TIER: dict[str, SafetyTier] = {
    "severe": SafetyTier.BLOCK,
    "moderate": SafetyTier.OVERRIDE_REQUIRED,
    "mild": SafetyTier.INFO,
}

_INTERACTION_SEVERITY_TIER: dict[str, SafetyTier] = {
    "contraindicated": SafetyTier.BLOCK,
    "major": SafetyTier.OVERRIDE_REQUIRED,
    "moderate": SafetyTier.OVERRIDE_REQUIRED,
    "minor": SafetyTier.INFO,
}


def classify_allergy_severity(severity: str) -> SafetyTier:
    """Pure function, no DB access: given `patient_allergies.severity`
    (expected: mild/moderate/severe, case-insensitive), return the tier.

    Fail-safe default: an unrecognized severity string (bad/legacy data,
    a typo written by a future migration) maps to `OVERRIDE_REQUIRED`
    rather than `INFO` or raising. This is a deliberate judgment call for a
    safety-critical checker -- silently treating unknown data as "no big
    deal" (INFO) risks waving through a real problem, and raising would
    500 the entire prescription flow over a data-quality issue elsewhere.
    Requiring an explicit override at least puts a human in the loop.
    """
    return _ALLERGY_SEVERITY_TIER.get(severity.strip().lower(), SafetyTier.OVERRIDE_REQUIRED)


def classify_interaction_severity(severity: str) -> SafetyTier:
    """Pure function, no DB access: given `drug_interactions.severity`
    (expected: contraindicated/major/moderate/minor, case-insensitive),
    return the tier. Same fail-safe-to-OVERRIDE_REQUIRED default as
    `classify_allergy_severity` -- see its docstring.
    """
    return _INTERACTION_SEVERITY_TIER.get(severity.strip().lower(), SafetyTier.OVERRIDE_REQUIRED)


@dataclass(frozen=True)
class SafetyFinding:
    """One safety-check hit, enough detail for a human (the prescribing
    doctor, or a reviewer reading an audit-log entry) to understand what
    fired without re-querying the DB.

    `drug_ids` holds one id for an allergy finding (the conflicting drug)
    or two for an interaction finding (both sides of the pair). `severity`
    is the raw string from the DB (e.g. "severe", "major") -- `tier` is the
    already-computed classification, kept alongside rather than requiring
    the caller to re-derive it.
    """

    source: Literal["allergy", "interaction"]
    severity: str
    tier: SafetyTier
    message: str
    drug_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class PrescriptionSafetyReport:
    """Bundles every finding from both checks -- not just the worst one.
    The Phase-2 endpoint needs the full picture to decide BLOCK vs.
    OVERRIDE_REQUIRED vs. proceed, and the UI needs INFO items surfaced
    even on an otherwise-clean success (per the PRP)."""

    allergy_findings: list[SafetyFinding] = field(default_factory=list)
    interaction_findings: list[SafetyFinding] = field(default_factory=list)

    @property
    def all_findings(self) -> list[SafetyFinding]:
        return [*self.allergy_findings, *self.interaction_findings]

    @property
    def highest_tier(self) -> SafetyTier | None:
        """The worst tier present across all findings, or `None` if there
        are no findings at all. `BLOCK` outranks `OVERRIDE_REQUIRED`
        outranks `INFO`, per the PRP's endpoint flow (any BLOCK anywhere ->
        reject outright; else any OVERRIDE_REQUIRED -> 409 unless
        overridden; else proceed, surfacing INFO)."""
        tiers = {finding.tier for finding in self.all_findings}
        if SafetyTier.BLOCK in tiers:
            return SafetyTier.BLOCK
        if SafetyTier.OVERRIDE_REQUIRED in tiers:
            return SafetyTier.OVERRIDE_REQUIRED
        if SafetyTier.INFO in tiers:
            return SafetyTier.INFO
        return None


def check_allergy_conflicts(
    db: Session, patient_id: uuid.UUID, drug_ids: list[uuid.UUID]
) -> list[SafetyFinding]:
    """Match every requested drug against the patient's recorded allergies.

    See the module docstring's "ALLERGY MATCHING" section for the exact
    matching rule (case-insensitive equality against `drug.name`,
    `drug.generic_name`, or `drug.interaction_class`) and its documented
    false-negative gap. This function does the DB queries; the actual
    match/classify logic is a small, explicit loop below rather than SQL,
    since it needs three separate case-insensitive comparisons per
    (drug, allergy) pair -- easier to read and to unit-test against fixed
    fixtures than an equivalent SQL `OR` chain would be.
    """
    if not drug_ids:
        return []

    allergies = db.execute(
        select(PatientAllergy).where(PatientAllergy.patient_id == patient_id)
    ).scalars().all()
    if not allergies:
        return []

    drugs = db.execute(select(Drug).where(Drug.id.in_(drug_ids))).scalars().all()

    findings: list[SafetyFinding] = []
    for drug in drugs:
        candidate_fields = {
            value.strip().lower()
            for value in (drug.name, drug.generic_name, drug.interaction_class)
            if value
        }
        for allergy in allergies:
            if allergy.substance.strip().lower() not in candidate_fields:
                continue
            tier = classify_allergy_severity(allergy.severity)
            message = (
                f"{drug.name} conflicts with recorded allergy to "
                f"'{allergy.substance}' ({allergy.severity})"
            )
            if allergy.reaction:
                message += f": {allergy.reaction}"
            findings.append(
                SafetyFinding(
                    source="allergy",
                    severity=allergy.severity,
                    tier=tier,
                    message=message,
                    drug_ids=(drug.id,),
                )
            )
    return findings


def _active_prescription_drug_ids(
    db: Session, patient_id: uuid.UUID, as_of: date | None = None
) -> set[uuid.UUID]:
    """Drug ids from this patient's currently-active prescriptions.

    "Active" = a `Prescription` with `status IN ('finalized', 'dispensed')`
    having at least one `PrescriptionItem` still within its course:
    `duration_days IS NULL` (ongoing/chronic, never expires) OR
    `created_at.date() + duration_days days >= as_of`.

    Date arithmetic is done in Python, not pushed into SQL: `created_at` is
    a `TIMESTAMPTZ` and `duration_days` a plain `SMALLINT`, and computing
    `created_at + (duration_days || ' days')::interval >= now()` in SQL
    would work on Postgres but ties this function's correctness to a
    dialect-specific expression that's harder to unit-test in isolation.
    The candidate row count per patient is small (their own prescription
    history), so fetching rows and filtering in Python is both simpler and
    cheap. `as_of` defaults to `date.today()` but is an explicit parameter
    so callers (tests) can pin a value instead of depending on wall-clock
    time.
    """
    as_of = as_of or date.today()

    stmt = (
        select(PrescriptionItem.drug_id, Prescription.created_at, PrescriptionItem.duration_days)
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .where(
            Prescription.patient_id == patient_id,
            Prescription.status.in_(_ACTIVE_PRESCRIPTION_STATUSES),
        )
    )

    active_drug_ids: set[uuid.UUID] = set()
    for drug_id, created_at, duration_days in db.execute(stmt).all():
        if duration_days is None:
            active_drug_ids.add(drug_id)
            continue
        course_end = created_at.date() + timedelta(days=duration_days)
        if course_end >= as_of:
            active_drug_ids.add(drug_id)
    return active_drug_ids


def _sorted_pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Canonical (sorted-by-string) ordering for a pair, used only to
    dedupe the *set of pairs we intend to check* -- NOT to assume anything
    about how `drug_interactions` itself is ordered on disk (it isn't, see
    `DrugInteraction`'s docstring). We still query both `(a, b)` and
    `(b, a)` against the table regardless of this local ordering.
    """
    return (a, b) if str(a) <= str(b) else (b, a)


def _candidate_interaction_pairs(
    new_drug_ids: list[uuid.UUID], existing_drug_ids: set[uuid.UUID]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Every unordered pair to check: each new drug against every other
    new drug in the SAME incoming request (two drugs prescribed together
    must be checked against each other, not just against prior
    prescriptions -- an easy case to accidentally skip), plus each new
    drug against every existing active drug.
    """
    unique_new = list(dict.fromkeys(new_drug_ids))  # de-dupe, preserve order
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    for i, drug_a in enumerate(unique_new):
        for drug_b in unique_new[i + 1 :]:
            pairs.add(_sorted_pair(drug_a, drug_b))
        for existing_drug in existing_drug_ids:
            if existing_drug != drug_a:
                pairs.add(_sorted_pair(drug_a, existing_drug))

    return pairs


def check_drug_interactions(
    db: Session,
    patient_id: uuid.UUID,
    drug_ids: list[uuid.UUID],
    *,
    as_of: date | None = None,
) -> list[SafetyFinding]:
    """Check the requested drugs against each other and against the
    patient's other currently-active prescriptions.

    Queries `drug_interactions` in BOTH `(drug_a_id, drug_b_id)` and
    `(drug_b_id, drug_a_id)` orderings for every candidate pair -- see
    `DrugInteraction`'s docstring for why there's no canonical order
    guarantee on that reference table.
    """
    if not drug_ids:
        return []

    existing_drug_ids = _active_prescription_drug_ids(db, patient_id, as_of=as_of)
    pairs = _candidate_interaction_pairs(drug_ids, existing_drug_ids)
    if not pairs:
        return []

    or_clauses = [
        or_(
            and_(DrugInteraction.drug_a_id == a, DrugInteraction.drug_b_id == b),
            and_(DrugInteraction.drug_a_id == b, DrugInteraction.drug_b_id == a),
        )
        for a, b in pairs
    ]
    interactions = db.execute(select(DrugInteraction).where(or_(*or_clauses))).scalars().all()
    if not interactions:
        return []

    involved_ids = {drug_id for pair in pairs for drug_id in pair}
    drug_names = {
        row.id: row.name
        for row in db.execute(select(Drug).where(Drug.id.in_(involved_ids))).scalars().all()
    }

    findings: list[SafetyFinding] = []
    for interaction in interactions:
        tier = classify_interaction_severity(interaction.severity)
        name_a = drug_names.get(interaction.drug_a_id, str(interaction.drug_a_id))
        name_b = drug_names.get(interaction.drug_b_id, str(interaction.drug_b_id))
        message = f"{name_a} + {name_b}: {interaction.severity} interaction"
        if interaction.description:
            message += f" -- {interaction.description}"
        findings.append(
            SafetyFinding(
                source="interaction",
                severity=interaction.severity,
                tier=tier,
                message=message,
                drug_ids=(interaction.drug_a_id, interaction.drug_b_id),
            )
        )
    return findings


def evaluate_prescription_safety(
    db: Session,
    patient_id: uuid.UUID,
    drug_ids: list[uuid.UUID],
    *,
    as_of: date | None = None,
) -> PrescriptionSafetyReport:
    """Entry point for Phase 2's `consultation_service.create_prescription`:
    runs both checks and bundles the full result. `as_of` is exposed for
    testability (see `_active_prescription_drug_ids`); production callers
    should leave it as `None` (defaults to `date.today()`).
    """
    return PrescriptionSafetyReport(
        allergy_findings=check_allergy_conflicts(db, patient_id, drug_ids),
        interaction_findings=check_drug_interactions(db, patient_id, drug_ids, as_of=as_of),
    )
