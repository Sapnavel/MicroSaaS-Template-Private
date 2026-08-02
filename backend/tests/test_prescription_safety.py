"""Unit + DB-backed tests for services/prescription_safety.py, per
PRPs/clinical-consultation-prescription-prp.md Phase 3.

Two kinds of tests live here, per Validation Gate 1's own split:
- Pure classification tests (`classify_allergy_severity`,
  `classify_interaction_severity`, `PrescriptionSafetyReport.highest_tier`)
  need no DB fixture at all -- they're plain functions over strings/dataclasses.
- `check_allergy_conflicts` / `check_drug_interactions` /
  `evaluate_prescription_safety` need the real Postgres fixtures (`db`,
  `patient`, `consultation`) since they query `drugs`/`drug_interactions`/
  `patient_allergies`/`prescriptions`/`prescription_items`.

Seeded reference data (database/seed_clinical_reference_data.sql) is applied
once against the test Postgres before this suite runs (see the module
docstring in tests/conftest.py for why `drugs`/`drug_interactions` are
deliberately NOT in `TRUNCATE_TABLES`) -- these tests look drug ids up by
name rather than hardcoding UUIDs. The exact seeded severities used below
were confirmed directly against the seed file / a live query, not assumed:

    Lanoxin (Digoxin) x Biaxin (Clarithromycin)   -> contraindicated -> BLOCK
    Coumadin (Warfarin) x Bayer Aspirin (Aspirin) -> major           -> OVERRIDE_REQUIRED
    Lasix (Furosemide) x Prinivil (Lisinopril)    -> moderate        -> OVERRIDE_REQUIRED
    Augmentin x Zithromax (Azithromycin)          -> minor           -> INFO
    Zocor (Simvastatin) x Biaxin (Clarithromycin) -> major           -> OVERRIDE_REQUIRED
"""

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models.consultation import Drug, Prescription, PrescriptionItem
from app.services.prescription_safety import (
    PrescriptionSafetyReport,
    SafetyFinding,
    SafetyTier,
    check_allergy_conflicts,
    check_drug_interactions,
    classify_allergy_severity,
    classify_interaction_severity,
    evaluate_prescription_safety,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drug_id(db, name: str) -> uuid.UUID:
    return db.execute(select(Drug.id).where(Drug.name == name)).scalar_one()


def _add_allergy(db, patient_id, substance: str, severity: str, reaction: str | None = None):
    from app.models.consultation import PatientAllergy

    allergy = PatientAllergy(patient_id=patient_id, substance=substance, severity=severity, reaction=reaction)
    db.add(allergy)
    db.commit()
    db.refresh(allergy)
    return allergy


def _add_finalized_prescription(db, consultation, patient_id, drug_id, duration_days, created_at=None):
    prescription = Prescription(
        consultation_id=consultation.id,
        patient_id=patient_id,
        status="finalized",
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(prescription)
    db.flush()
    item = PrescriptionItem(
        prescription_id=prescription.id,
        drug_id=drug_id,
        dosage="1 tab",
        frequency="daily",
        duration_days=duration_days,
    )
    db.add(item)
    db.commit()
    return prescription, item


# ---------------------------------------------------------------------------
# classify_allergy_severity / classify_interaction_severity (pure, no DB)
# ---------------------------------------------------------------------------


def test_classify_allergy_severity_maps_every_known_tier():
    assert classify_allergy_severity("severe") == SafetyTier.BLOCK
    assert classify_allergy_severity("moderate") == SafetyTier.OVERRIDE_REQUIRED
    assert classify_allergy_severity("mild") == SafetyTier.INFO


def test_classify_allergy_severity_is_case_insensitive():
    assert classify_allergy_severity("SEVERE") == SafetyTier.BLOCK
    assert classify_allergy_severity("Moderate") == SafetyTier.OVERRIDE_REQUIRED
    assert classify_allergy_severity("MILD") == SafetyTier.INFO
    assert classify_allergy_severity("  mild  ") == SafetyTier.INFO


def test_classify_allergy_severity_unrecognized_string_fails_safe_to_override_required():
    assert classify_allergy_severity("bogus-severity") == SafetyTier.OVERRIDE_REQUIRED
    assert classify_allergy_severity("") == SafetyTier.OVERRIDE_REQUIRED


def test_classify_interaction_severity_maps_every_known_tier():
    assert classify_interaction_severity("contraindicated") == SafetyTier.BLOCK
    assert classify_interaction_severity("major") == SafetyTier.OVERRIDE_REQUIRED
    assert classify_interaction_severity("moderate") == SafetyTier.OVERRIDE_REQUIRED
    assert classify_interaction_severity("minor") == SafetyTier.INFO


def test_classify_interaction_severity_is_case_insensitive():
    assert classify_interaction_severity("CONTRAINDICATED") == SafetyTier.BLOCK
    assert classify_interaction_severity("Major") == SafetyTier.OVERRIDE_REQUIRED
    assert classify_interaction_severity("MINOR") == SafetyTier.INFO


def test_classify_interaction_severity_unrecognized_string_fails_safe_to_override_required():
    assert classify_interaction_severity("catastrophic") == SafetyTier.OVERRIDE_REQUIRED


# ---------------------------------------------------------------------------
# PrescriptionSafetyReport.highest_tier -- pure, no DB
# ---------------------------------------------------------------------------


def _finding(tier: SafetyTier, source="interaction") -> SafetyFinding:
    return SafetyFinding(source=source, severity="x", tier=tier, message="msg", drug_ids=(uuid.uuid4(),))


def test_highest_tier_is_none_with_no_findings():
    report = PrescriptionSafetyReport()
    assert report.highest_tier is None


def test_highest_tier_block_outranks_everything():
    report = PrescriptionSafetyReport(
        allergy_findings=[_finding(SafetyTier.INFO, "allergy")],
        interaction_findings=[_finding(SafetyTier.OVERRIDE_REQUIRED), _finding(SafetyTier.BLOCK)],
    )
    assert report.highest_tier == SafetyTier.BLOCK


def test_highest_tier_override_required_outranks_info():
    report = PrescriptionSafetyReport(
        allergy_findings=[_finding(SafetyTier.INFO, "allergy")],
        interaction_findings=[_finding(SafetyTier.OVERRIDE_REQUIRED)],
    )
    assert report.highest_tier == SafetyTier.OVERRIDE_REQUIRED


def test_highest_tier_info_when_only_info_findings():
    report = PrescriptionSafetyReport(interaction_findings=[_finding(SafetyTier.INFO)])
    assert report.highest_tier == SafetyTier.INFO


# ---------------------------------------------------------------------------
# check_allergy_conflicts
# ---------------------------------------------------------------------------


def test_check_allergy_conflicts_matches_on_drug_name(db, patient):
    _add_allergy(db, patient.id, "Coumadin", "severe")
    coumadin_id = _drug_id(db, "Coumadin")

    findings = check_allergy_conflicts(db, patient.id, [coumadin_id])

    assert len(findings) == 1
    assert findings[0].source == "allergy"
    assert findings[0].tier == SafetyTier.BLOCK
    assert findings[0].drug_ids == (coumadin_id,)


def test_check_allergy_conflicts_matches_on_generic_name(db, patient):
    # Coumadin's generic_name is "Warfarin" -- allergy recorded against the
    # generic name, not the brand name, must still match.
    _add_allergy(db, patient.id, "Warfarin", "moderate")
    coumadin_id = _drug_id(db, "Coumadin")

    findings = check_allergy_conflicts(db, patient.id, [coumadin_id])

    assert len(findings) == 1
    assert findings[0].tier == SafetyTier.OVERRIDE_REQUIRED


def test_check_allergy_conflicts_matches_on_interaction_class(db, patient):
    # "nsaid" is the interaction_class shared by Bayer Aspirin/Advil/Aleve --
    # per the seed file's own documented example, this is the one path
    # where a class-level allergy string IS caught.
    _add_allergy(db, patient.id, "nsaid", "mild")
    aspirin_id = _drug_id(db, "Bayer Aspirin")
    advil_id = _drug_id(db, "Advil")

    findings = check_allergy_conflicts(db, patient.id, [aspirin_id, advil_id])

    assert len(findings) == 2
    assert {f.tier for f in findings} == {SafetyTier.INFO}


def test_check_allergy_conflicts_is_case_insensitive(db, patient):
    _add_allergy(db, patient.id, "COUMADIN", "severe")
    coumadin_id = _drug_id(db, "Coumadin")

    findings = check_allergy_conflicts(db, patient.id, [coumadin_id])
    assert len(findings) == 1


def test_check_allergy_conflicts_class_level_substance_does_not_match_documented_gap(db, patient):
    """Documented, deliberate limitation (see prescription_safety.py's module
    docstring): a patient recorded as allergic to 'Penicillin' (a class name)
    does NOT match Amoxil/Augmentin, since neither drug's name, generic_name,
    or interaction_class ('beta-lactam') is literally the string
    'Penicillin'. This is NOT a bug to fix -- assert the absence of a
    finding."""
    _add_allergy(db, patient.id, "Penicillin", "severe")
    amoxil_id = _drug_id(db, "Amoxil")
    augmentin_id = _drug_id(db, "Augmentin")

    findings = check_allergy_conflicts(db, patient.id, [amoxil_id, augmentin_id])

    assert findings == []


# ---------------------------------------------------------------------------
# check_drug_interactions
# ---------------------------------------------------------------------------


def test_check_drug_interactions_finds_known_pair_one_direction(db, patient):
    coumadin_id = _drug_id(db, "Coumadin")
    aspirin_id = _drug_id(db, "Bayer Aspirin")

    findings = check_drug_interactions(db, patient.id, [coumadin_id, aspirin_id])

    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert findings[0].tier == SafetyTier.OVERRIDE_REQUIRED
    assert set(findings[0].drug_ids) == {coumadin_id, aspirin_id}


def test_check_drug_interactions_finds_same_pair_queried_reversed_order(db, patient):
    """Same pair as above, request list reversed -- confirms the lookup
    isn't accidentally order-sensitive despite `drug_interactions` having no
    canonical (drug_a_id, drug_b_id) ordering guarantee."""
    coumadin_id = _drug_id(db, "Coumadin")
    aspirin_id = _drug_id(db, "Bayer Aspirin")

    findings = check_drug_interactions(db, patient.id, [aspirin_id, coumadin_id])

    assert len(findings) == 1
    assert findings[0].severity == "major"


def test_check_drug_interactions_checks_two_new_drugs_against_each_other(db, patient):
    """Two drugs in the SAME incoming request (no prior prescription at all)
    must be checked against each other -- the PRP explicitly calls out that
    this is easy to accidentally skip if only new x existing pairs are
    considered."""
    zocor_id = _drug_id(db, "Zocor")
    biaxin_id = _drug_id(db, "Biaxin")

    findings = check_drug_interactions(db, patient.id, [zocor_id, biaxin_id])

    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert set(findings[0].drug_ids) == {zocor_id, biaxin_id}


def test_check_drug_interactions_excludes_expired_active_prescription(db, patient, consultation):
    """An existing finalized prescription whose `duration_days` course has
    already ended relative to `as_of` must NOT count as "active" -- so its
    drug should not be checked against the new drug at all."""
    coumadin_id = _drug_id(db, "Coumadin")
    aspirin_id = _drug_id(db, "Bayer Aspirin")

    as_of = date.today()
    old_created_at = datetime.now(timezone.utc) - timedelta(days=30)
    # duration_days=5 from 30 days ago -> course ended 25 days ago, well
    # before `as_of`.
    _add_finalized_prescription(db, consultation, patient.id, coumadin_id, duration_days=5, created_at=old_created_at)

    findings = check_drug_interactions(db, patient.id, [aspirin_id], as_of=as_of)

    assert findings == []


def test_check_drug_interactions_null_duration_days_is_always_active(db, patient, consultation):
    """`duration_days IS NULL` means an ongoing/chronic medication that
    never expires -- must always count as active, no matter how old
    `created_at` is."""
    coumadin_id = _drug_id(db, "Coumadin")
    aspirin_id = _drug_id(db, "Bayer Aspirin")

    as_of = date.today()
    old_created_at = datetime.now(timezone.utc) - timedelta(days=3650)
    _add_finalized_prescription(
        db, consultation, patient.id, coumadin_id, duration_days=None, created_at=old_created_at
    )

    findings = check_drug_interactions(db, patient.id, [aspirin_id], as_of=as_of)

    assert len(findings) == 1
    assert findings[0].severity == "major"


def test_check_drug_interactions_no_drugs_returns_empty(db, patient):
    assert check_drug_interactions(db, patient.id, []) == []


# ---------------------------------------------------------------------------
# evaluate_prescription_safety
# ---------------------------------------------------------------------------


def test_evaluate_prescription_safety_bundles_both_checks(db, patient):
    _add_allergy(db, patient.id, "nsaid", "mild")
    coumadin_id = _drug_id(db, "Coumadin")
    aspirin_id = _drug_id(db, "Bayer Aspirin")

    report = evaluate_prescription_safety(db, patient.id, [coumadin_id, aspirin_id])

    # One allergy finding (aspirin is nsaid-class, mild -> INFO) and one
    # interaction finding (Coumadin x Bayer Aspirin, major -> OVERRIDE_REQUIRED).
    assert len(report.allergy_findings) == 1
    assert report.allergy_findings[0].tier == SafetyTier.INFO
    assert len(report.interaction_findings) == 1
    assert report.interaction_findings[0].tier == SafetyTier.OVERRIDE_REQUIRED
    assert report.highest_tier == SafetyTier.OVERRIDE_REQUIRED


def test_evaluate_prescription_safety_clean_drug_has_no_findings(db, patient):
    metformin_id = _drug_id(db, "Glucophage")

    report = evaluate_prescription_safety(db, patient.id, [metformin_id])

    assert report.all_findings == []
    assert report.highest_tier is None


def test_evaluate_prescription_safety_block_tier_from_contraindicated_interaction(db, patient):
    digoxin_id = _drug_id(db, "Lanoxin")
    clarithromycin_id = _drug_id(db, "Biaxin")

    report = evaluate_prescription_safety(db, patient.id, [digoxin_id, clarithromycin_id])

    assert report.highest_tier == SafetyTier.BLOCK
    assert any(f.severity == "contraindicated" for f in report.interaction_findings)


def test_evaluate_prescription_safety_block_tier_from_severe_allergy(db, patient):
    _add_allergy(db, patient.id, "Coumadin", "severe")
    coumadin_id = _drug_id(db, "Coumadin")

    report = evaluate_prescription_safety(db, patient.id, [coumadin_id])

    assert report.highest_tier == SafetyTier.BLOCK
