"""Unit tests for services/patient_matching.py, per
PRPs/patient-master-index-prp.md Phase 3.

`score_candidate` / `normalize_name` / `normalize_phone_for_matching` are
pure Python (no DB) -- tested here with plain pytest, no `db`/`client`
fixtures. `find_deterministic_match` / `scan_for_duplicates` need a real
Postgres (trigram `similarity()` is a Postgres function, and
`find_deterministic_match` queries the `patients` table directly) -- those
are exercised here too, against the real test Postgres via the `db` fixture
from conftest.py, rather than deferred to test_patients.py, since they are
still "matching engine" behavior rather than endpoint behavior.
"""

import uuid
from datetime import date

from app.core.encryption import deterministic_hash
from app.models.patient import Patient
from app.services import patient_matching
from app.services.patient_matching import MatchFields, score_candidate

# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


def test_normalize_name_collapses_internal_whitespace():
    assert patient_matching.normalize_name("John   Q  Public") == "john q public"


def test_normalize_name_strips_leading_and_trailing_whitespace():
    assert patient_matching.normalize_name("  Jane Doe  ") == "jane doe"


def test_normalize_name_case_folds():
    assert patient_matching.normalize_name("JANE DOE") == "jane doe"


def test_normalize_name_combines_all_three_rules():
    assert patient_matching.normalize_name("  JANE   Q.   DOE  ") == "jane q. doe"


# ---------------------------------------------------------------------------
# normalize_phone_for_matching
# ---------------------------------------------------------------------------


def test_normalize_phone_strips_non_digit_characters():
    assert patient_matching.normalize_phone_for_matching("(555) 123-4567") == "5551234567"


def test_normalize_phone_already_digits_only_is_unchanged():
    assert patient_matching.normalize_phone_for_matching("5551234567") == "5551234567"


def test_normalize_phone_different_formats_of_same_number_match():
    """The exact regression this function exists for (fix #2 in the phase
    brief): "555-123-4567" and "5551234567" must normalize identically so
    they hash identically via deterministic_hash()."""
    a = patient_matching.normalize_phone_for_matching("555-123-4567")
    b = patient_matching.normalize_phone_for_matching("5551234567")
    assert a == b == "5551234567"
    assert deterministic_hash(a) == deterministic_hash(b)


def test_normalize_phone_strips_plus_and_spaces_international_format():
    assert patient_matching.normalize_phone_for_matching("+1 555 123 4567") == "15551234567"


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------


def test_score_candidate_phone_only_exact_match():
    subject = MatchFields(phone_hash="hash-a", dob=None, full_name=None, national_id_hash=None)
    candidate = MatchFields(phone_hash="hash-a", dob=None, full_name=None, national_id_hash=None)

    score, reason = score_candidate(subject, candidate, name_similarity=0.0)

    assert score == 0.40
    assert reason["phone_hash"]["matched"] is True
    assert reason["phone_hash"]["weight"] == 0.40
    assert "national_id_hash" not in reason
    assert "dob" not in reason
    assert "full_name" not in reason


def test_score_candidate_national_id_only_exact_match():
    subject = MatchFields(national_id_hash="nid-hash-a")
    candidate = MatchFields(national_id_hash="nid-hash-a")

    score, reason = score_candidate(subject, candidate, name_similarity=0.0)

    assert score == 0.45
    assert reason["national_id_hash"]["matched"] is True
    assert reason["national_id_hash"]["weight"] == 0.45


def test_score_candidate_dob_only_match():
    subject = MatchFields(dob=date(1990, 1, 1))
    candidate = MatchFields(dob=date(1990, 1, 1))

    score, reason = score_candidate(subject, candidate, name_similarity=0.0)

    assert score == 0.15
    assert reason["dob"]["matched"] is True
    assert reason["dob"]["weight"] == 0.15


def test_score_candidate_name_similarity_only_full_similarity():
    subject = MatchFields()
    candidate = MatchFields()

    score, reason = score_candidate(subject, candidate, name_similarity=1.0)

    assert score == 0.25
    assert reason["full_name"]["similarity"] == 1.0
    assert reason["full_name"]["weight_applied"] == 0.25


def test_score_candidate_name_similarity_only_partial_similarity():
    subject = MatchFields()
    candidate = MatchFields()

    score, reason = score_candidate(subject, candidate, name_similarity=0.6667)

    # 0.6667 * 0.25 = 0.166675, rounded to 4 places by the implementation.
    assert score == round(0.6667 * 0.25, 4)
    assert reason["full_name"]["similarity"] == 0.6667


def test_score_candidate_name_similarity_below_scan_threshold_still_scores_if_passed_directly():
    """score_candidate itself doesn't enforce the 0.4 SQL-prefilter floor --
    that's scan_for_duplicates' job via the WHERE clause. Any positive
    similarity contributes proportionally."""
    subject = MatchFields()
    candidate = MatchFields()

    score, reason = score_candidate(subject, candidate, name_similarity=0.1)

    assert score == round(0.1 * 0.25, 4)


def test_score_candidate_zero_name_similarity_contributes_nothing_and_no_reason_key():
    subject = MatchFields(dob=date(1990, 1, 1))
    candidate = MatchFields(dob=date(1990, 1, 1))

    score, reason = score_candidate(subject, candidate, name_similarity=0.0)

    assert score == 0.15
    assert "full_name" not in reason


def test_score_candidate_exact_everything_caps_at_one_point_zero():
    subject = MatchFields(
        phone_hash="hash-a", national_id_hash="nid-a", dob=date(1990, 1, 1), full_name="Jane Doe"
    )
    candidate = MatchFields(
        phone_hash="hash-a", national_id_hash="nid-a", dob=date(1990, 1, 1), full_name="Jane Doe"
    )

    # 0.40 + 0.45 + 0.15 + (1.0 * 0.25) = 1.25 uncapped -> capped to 1.0.
    score, reason = score_candidate(subject, candidate, name_similarity=1.0)

    assert score == 1.0
    assert reason["total_score"] == 1.0


def test_score_candidate_national_id_present_on_only_one_side_is_ignored_not_penalized():
    """The PRP is explicit: national_id_hash weight only applies "if both
    non-null" -- a national ID present on only one side must contribute
    exactly 0, not be treated as a mismatch that reduces the score below
    what the other signals alone would produce."""
    subject_with_nid = MatchFields(dob=date(1990, 1, 1), national_id_hash="nid-a")
    subject_without_nid = MatchFields(dob=date(1990, 1, 1), national_id_hash=None)
    candidate_without_nid = MatchFields(dob=date(1990, 1, 1), national_id_hash=None)

    score_with, reason_with = score_candidate(subject_with_nid, candidate_without_nid, name_similarity=0.0)
    score_without, reason_without = score_candidate(
        subject_without_nid, candidate_without_nid, name_similarity=0.0
    )

    assert score_with == score_without == 0.15
    assert "national_id_hash" not in reason_with
    assert "national_id_hash" not in reason_without


def test_score_candidate_combined_signals_sum_correctly():
    """DOB + name-typo, no phone/national_id -- the exact scenario the
    0.25 threshold correction (fix #1) exists to catch. Uses the real
    similarity value measured against Postgres for 'Jonathan Smith' vs
    'Jonathon Smith' (0.6666667, see test_patients.py's duplicate-candidate
    regression test for where that number comes from)."""
    subject = MatchFields(dob=date(1985, 6, 15), phone_hash="hash-subject", full_name="Jonathan Smith")
    candidate = MatchFields(dob=date(1985, 6, 15), phone_hash="hash-different", full_name="Jonathon Smith")

    score, reason = score_candidate(subject, candidate, name_similarity=0.6666667)

    expected_name_contribution = round(0.6666667 * 0.25, 4)
    expected_total = round(0.15 + expected_name_contribution, 4)
    assert score == expected_total
    assert score >= patient_matching.DUPLICATE_CANDIDATE_SCORE_THRESHOLD
    # And critically: this score would NOT have cleared the PRP draft's
    # original (incorrect) 0.5 threshold, which is exactly why it was
    # corrected to 0.25 -- see patient_matching.py's module-level comment.
    assert score < 0.5


def test_duplicate_candidate_score_threshold_is_0_25_not_0_5():
    """Direct regression test for the threshold-correction fix itself (fix
    #1 in the phase brief): pins the constant so a future edit silently
    reverting to the PRP draft's mathematically-unreachable 0.5 is caught
    immediately, before it ever reaches the DB-level regression test in
    test_patients.py."""
    assert patient_matching.DUPLICATE_CANDIDATE_SCORE_THRESHOLD == 0.25


# ---------------------------------------------------------------------------
# find_deterministic_match -- needs a real Postgres (queries `patients`
# directly), so uses the `db` fixture from conftest.py.
# ---------------------------------------------------------------------------


def _make_patient(**overrides) -> Patient:
    """Build an unpersisted `Patient` for direct ORM insertion in these
    matching-engine tests. `phone_hash` is (re)computed from the final
    `phone` value -- via the same `normalize_phone_for_matching` +
    `deterministic_hash` pipeline `patient_service.create_patient` uses --
    UNLESS the caller explicitly overrides `phone_hash` too, so tests that
    only override `phone` never end up with a hash/phone mismatch (which
    would silently make two differently-phoned fixtures compare as a phone
    match, or vice versa)."""
    defaults = dict(
        mrn=f"MRN{uuid.uuid4().hex[:10].upper()}",
        full_name="Alice Wonderland",
        dob=date(1988, 3, 12),
        sex="F",
        phone="555-000-0001",
        national_id=None,
        address=None,
        allergies_note=None,
        national_id_hash=None,
    )
    defaults.update(overrides)
    if "phone_hash" not in overrides:
        defaults["phone_hash"] = deterministic_hash(
            patient_matching.normalize_phone_for_matching(defaults["phone"])
        )
    return Patient(**defaults)


def test_find_deterministic_match_returns_none_when_no_match(db):
    existing = _make_patient(full_name="Someone Else", phone="555-999-9999")
    db.add(existing)
    db.commit()

    result = patient_matching.find_deterministic_match(
        db,
        phone_hash=deterministic_hash("555-111-1111"),
        national_id_hash=None,
        dob=date(2000, 1, 1),
        full_name="Nobody Matching",
    )
    assert result is None


def test_find_deterministic_match_matches_on_phone_hash(db):
    phone_hash = deterministic_hash(patient_matching.normalize_phone_for_matching("555-222-3333"))
    existing = _make_patient(phone_hash=phone_hash, dob=date(1975, 5, 5), full_name="Bob Builder")
    db.add(existing)
    db.commit()

    result = patient_matching.find_deterministic_match(
        db,
        phone_hash=phone_hash,
        national_id_hash=None,
        dob=date(1999, 9, 9),  # deliberately different -- phone alone must match
        full_name="Totally Different Name",
    )
    assert result is not None
    assert result.id == existing.id


def test_find_deterministic_match_matches_on_national_id_hash(db):
    nid_hash = deterministic_hash("A1234567")
    existing = _make_patient(national_id_hash=nid_hash, dob=date(1970, 1, 1), full_name="Carla Diaz")
    db.add(existing)
    db.commit()

    result = patient_matching.find_deterministic_match(
        db,
        phone_hash=deterministic_hash("000-000-0000"),
        national_id_hash=nid_hash,
        dob=date(2001, 1, 1),
        full_name="Different Name Entirely",
    )
    assert result is not None
    assert result.id == existing.id


def test_find_deterministic_match_matches_on_dob_and_normalized_name(db):
    existing = _make_patient(dob=date(1992, 7, 4), full_name="  Denise   Yu ", phone="555-444-4444")
    db.add(existing)
    db.commit()

    result = patient_matching.find_deterministic_match(
        db,
        phone_hash=deterministic_hash("555-000-0000"),  # different phone
        national_id_hash=None,
        dob=date(1992, 7, 4),
        full_name="DENISE YU",  # same after normalize_name, different casing/whitespace
    )
    assert result is not None
    assert result.id == existing.id


def test_find_deterministic_match_excludes_already_merged_patients(db):
    # Deliberately a DIFFERENT dob/full_name than the loser below: this
    # isolates the phone_hash-only match path from the dob+name path, so
    # the only way this search could hit is via the (tombstoned) loser's
    # phone_hash -- if survivor and loser shared dob+name too, the search
    # would still find a match (the still-active survivor), which would
    # pass for the wrong reason and not actually prove tombstoning works.
    survivor = _make_patient(dob=date(1955, 1, 1), full_name="Survivor Unrelated", phone="555-777-7777")
    db.add(survivor)
    db.commit()
    db.refresh(survivor)

    loser_phone_hash = deterministic_hash(patient_matching.normalize_phone_for_matching("555-888-8888"))
    loser = _make_patient(
        dob=date(1960, 2, 2),
        full_name="Ed Original",
        phone="555-888-8888",
        phone_hash=loser_phone_hash,
        merged_into_id=survivor.id,
    )
    db.add(loser)
    db.commit()

    # Searching by the tombstoned loser's own phone_hash must not resurrect it.
    result = patient_matching.find_deterministic_match(
        db,
        phone_hash=loser_phone_hash,
        national_id_hash=None,
        dob=date(1960, 2, 2),
        full_name="Ed Original",
    )
    assert result is None


def test_find_deterministic_match_excludes_given_patient_id(db):
    existing = _make_patient(dob=date(1980, 8, 8), full_name="Fiona Green", phone="555-666-6666")
    db.add(existing)
    db.commit()
    db.refresh(existing)

    result = patient_matching.find_deterministic_match(
        db,
        phone_hash=existing.phone_hash,
        national_id_hash=None,
        dob=existing.dob,
        full_name=existing.full_name,
        exclude_patient_id=existing.id,
    )
    assert result is None


# ---------------------------------------------------------------------------
# scan_for_duplicates -- also needs Postgres (pg_trgm similarity()).
# ---------------------------------------------------------------------------


def test_scan_for_duplicates_finds_dob_and_name_typo_candidate(db):
    """DB-level companion to test_score_candidate_combined_signals_sum_correctly
    above: confirms the SQL prefilter + scoring pipeline together surface a
    same-DOB, typo'd-name, different-phone pair as a candidate."""
    existing = _make_patient(
        dob=date(1985, 6, 15), full_name="Jonathan Smith", phone="555-100-1000"
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    new_patient = _make_patient(
        dob=date(1985, 6, 15), full_name="Jonathon Smith", phone="555-200-2000"
    )
    db.add(new_patient)
    db.commit()
    db.refresh(new_patient)

    candidates = patient_matching.scan_for_duplicates(db, new_patient)

    assert len(candidates) == 1
    candidate_patient, score, reason = candidates[0]
    assert candidate_patient.id == existing.id
    assert score >= patient_matching.DUPLICATE_CANDIDATE_SCORE_THRESHOLD
    assert "full_name" in reason
    assert "dob" in reason
    assert "phone_hash" not in reason  # different phones -- must not contribute


def test_scan_for_duplicates_excludes_self_and_dissimilar_patients(db):
    unrelated = _make_patient(dob=date(1985, 6, 15), full_name="Zachary Totally Unrelated", phone="555-300-3000")
    db.add(unrelated)
    db.commit()

    subject = _make_patient(dob=date(1985, 6, 15), full_name="Jonathan Smith", phone="555-400-4000")
    db.add(subject)
    db.commit()
    db.refresh(subject)

    candidates = patient_matching.scan_for_duplicates(db, subject)

    candidate_ids = {c[0].id for c in candidates}
    assert subject.id not in candidate_ids
    assert unrelated.id not in candidate_ids  # similarity() should be below the 0.4 scan floor
