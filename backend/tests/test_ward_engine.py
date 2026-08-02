"""Tests for `app.services.ward_engine` (PRPs/ward-bed-ot-module-prp.md,
Phase 3). Runs against a real Postgres + Redis (see tests/conftest.py's
module docstring), reusing fixtures from prior modules' TEST-AGENTs (`db`,
`branch`, `other_branch`, `staff_user`/`doctor_record` -- a "doctor" role
caller wired to a real `Doctor` row, reused here as a surgeon --
`other_doctor_user`/`other_doctor_record` -- a second, independently-wired
doctor, reused here as a second surgeon -- `room`, `patient`,
`system_admin_user`) plus this module's own `ward`/`bed`/`other_bed`/
`ot_room`/`other_ot_room`/`other_branch_ward`/`other_branch_bed`.

These are direct engine-level tests (calling `ward_engine.*` functions
against a real `Session`, not through the HTTP layer) -- router-level
status-code-mapping and authorize()/RBAC tests live in
tests/test_wards_router.py. This split mirrors test_pharmacy_engine.py
(pure-function unit tests) / test_pharmacy.py (integration) for the
Pharmacy module, except every function here needs a real DB (locks + EXCLUDE
constraints), so this file is not "pure" the way test_pharmacy_engine.py's
`allocate_fefo` tests are -- it's the DB-backed analogue of that split.
"""

import ast
import inspect
import itertools
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.locking import LockAcquisitionError
from app.models.appointment import Appointment, AppointmentStatus
from app.models.audit import AuditLog
from app.models.patient import Patient
from app.models.ward import Admission, Bed, BedStatus
from app.services import ward_engine
from app.services.ward_engine import (
    AdmissionNotFoundError,
    AlreadyDischargedError,
    BedConflictError,
    BedNotAvailableError,
    BedNotFoundError,
    IllegalBedStatusTransition,
    OTConflictError,
)
from tests.conftest import TestSessionLocal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_patient(db) -> Patient:
    """A second/third `Patient` row, built directly via the ORM -- the same
    "no need for the dedup-matching machinery" rationale conftest.py's own
    `patient` fixture docstring gives, just parameterized so tests needing
    more than one patient (concurrent admissions, transfer) aren't all
    forced to share conftest.py's single `patient` fixture."""
    p = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Ward Test Patient",
        dob=datetime(1980, 1, 1).date(),
        sex="F",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _fresh_bed_status(session, bed_id) -> BedStatus:
    return session.execute(select(Bed.status).where(Bed.id == bed_id)).scalar_one()


def _fresh_admission(session, admission_id) -> Admission:
    return session.execute(select(Admission).where(Admission.id == admission_id)).scalar_one()


def _source_without_docstring(fn) -> str:
    """`inspect.getsource(fn)` includes the function's own docstring, which
    in this module narrates the exact fix (H1/H2) in prose -- often
    mentioning the very identifiers/calls (`lock_manager.acquire_all`,
    `discharged_at is not None`) the source-inspection assertions below are
    trying to locate in the CODE. Strips the leading docstring via `ast` so
    those assertions can't be fooled by matching narrative text instead of
    the real statement."""
    source = inspect.getsource(fn)
    tree = ast.parse(source)
    func_def = tree.body[0]
    if (
        func_def.body
        and isinstance(func_def.body[0], ast.Expr)
        and isinstance(func_def.body[0].value, ast.Constant)
        and isinstance(func_def.body[0].value.value, str)
    ):
        doc_end_line = func_def.body[0].end_lineno
        return "\n".join(source.splitlines()[doc_end_line:])
    return source


# ---------------------------------------------------------------------------
# admit_patient
# ---------------------------------------------------------------------------


def test_admit_patient_happy_path(db, bed, patient, staff_user):
    start = datetime.now(timezone.utc)

    admission = ward_engine.admit_patient(
        db, patient_id=patient.id, bed_id=bed.id, start_time=start, admitted_by=staff_user.id
    )

    assert admission.patient_id == patient.id
    assert admission.bed_id == bed.id
    assert admission.discharged_at is None
    assert admission.stay_range.upper is None  # open-ended at admit time

    assert _fresh_bed_status(db, bed.id) == BedStatus.occupied

    audit_row = db.execute(
        select(AuditLog).where(AuditLog.action == "ward.admitted", AuditLog.resource_id == str(admission.id))
    ).scalar_one()
    assert audit_row.event_metadata["patient_id"] == str(patient.id)
    assert audit_row.event_metadata["bed_id"] == str(bed.id)


def test_admit_patient_bed_not_found(db, patient, staff_user):
    with pytest.raises(BedNotFoundError):
        ward_engine.admit_patient(
            db,
            patient_id=patient.id,
            bed_id=uuid.uuid4(),
            start_time=datetime.now(timezone.utc),
            admitted_by=staff_user.id,
        )


@pytest.mark.parametrize("blocking_status", [BedStatus.occupied, BedStatus.cleaning, BedStatus.blocked])
def test_admit_patient_bed_not_available_fast_path(db, bed, patient, staff_user, blocking_status):
    bed_row = db.get(Bed, bed.id)
    bed_row.status = blocking_status
    db.add(bed_row)
    db.commit()

    with pytest.raises(BedNotAvailableError):
        ward_engine.admit_patient(
            db,
            patient_id=patient.id,
            bed_id=bed.id,
            start_time=datetime.now(timezone.utc),
            admitted_by=staff_user.id,
        )

    # the fast-path rejection must not have mutated anything.
    assert _fresh_bed_status(db, bed.id) == blocking_status


def test_admit_patient_bed_conflict_via_exclude_constraint_bypassing_fast_path(db, bed, patient, staff_user):
    """`BedConflictError`'s whole reason to exist: `Bed.status` can drift
    from reality (bypassed by direct DB manipulation) -- this simulates that
    drift by resetting `bed.status` back to `available` while an active,
    undischarged admission still occupies it, so the fast-path check
    incorrectly passes and only the EXCLUDE constraint catches the real
    overlap."""
    first_patient = _make_patient(db)
    ward_engine.admit_patient(
        db,
        patient_id=first_patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc),
        admitted_by=staff_user.id,
    )

    drifted_bed = db.get(Bed, bed.id)
    drifted_bed.status = BedStatus.available
    db.add(drifted_bed)
    db.commit()

    with pytest.raises(BedConflictError):
        ward_engine.admit_patient(
            db,
            patient_id=patient.id,
            bed_id=bed.id,
            start_time=datetime.now(timezone.utc),
            admitted_by=staff_user.id,
        )

    # exactly one admission row for this bed -- the conflicting insert never
    # actually landed.
    rows = db.execute(select(Admission).where(Admission.bed_id == bed.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].patient_id == first_patient.id


def test_admit_patient_concurrent_admissions_for_same_bed_only_one_succeeds(bed, staff_user):
    """Genuine two-connection concurrency test (separate Postgres
    connections via `TestSessionLocal`, on separate threads), same style as
    test_pharmacy.py's `test_dispense_concurrent_requests_do_not_oversell`:
    two callers race `admit_patient` for the SAME bed. The Redis lock
    serializes the two attempts; whichever loses the race must see a clean
    rejection (fast-path `BedNotAvailableError` once the winner's status
    update is visible, or `BedConflictError` from the EXCLUDE constraint) --
    never a silent second admission for the same bed."""
    setup_db = TestSessionLocal()
    try:
        patient_a = _make_patient(setup_db)
        patient_b = _make_patient(setup_db)
        patient_a_id, patient_b_id = patient_a.id, patient_b.id
        bed_id, staff_id = bed.id, staff_user.id
    finally:
        setup_db.close()

    start = datetime.now(timezone.utc)
    results: dict[str, object] = {}

    def _admit(key: str, patient_id) -> None:
        conn = TestSessionLocal()
        try:
            admission = ward_engine.admit_patient(
                conn, patient_id=patient_id, bed_id=bed_id, start_time=start, admitted_by=staff_id
            )
            results[f"{key}_admission_id"] = admission.id
        except (BedNotAvailableError, BedConflictError) as exc:
            results[f"{key}_error"] = exc
        finally:
            conn.close()

    t_a = threading.Thread(target=_admit, args=("a", patient_a_id))
    t_b = threading.Thread(target=_admit, args=("b", patient_b_id))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    successes = [k for k in results if k.endswith("_admission_id")]
    errors = [k for k in results if k.endswith("_error")]
    assert len(successes) == 1, results
    assert len(errors) == 1, results

    verify_db = TestSessionLocal()
    try:
        rows = verify_db.execute(select(Admission).where(Admission.bed_id == bed_id)).scalars().all()
        assert len(rows) == 1
    finally:
        verify_db.close()


# ---------------------------------------------------------------------------
# discharge_patient
# ---------------------------------------------------------------------------


def test_discharge_patient_happy_path(db, bed, patient, staff_user):
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )

    updated = ward_engine.discharge_patient(db, admission_id=admission.id, discharged_by=staff_user.id)

    assert updated.discharged_at is not None
    assert updated.stay_range.upper is not None  # range closed, not left open

    assert _fresh_bed_status(db, bed.id) == BedStatus.cleaning  # NOT available -- see design decision #2

    audit_row = db.execute(
        select(AuditLog).where(AuditLog.action == "ward.discharged", AuditLog.resource_id == str(admission.id))
    ).scalar_one()
    assert audit_row is not None


def test_discharge_patient_not_found(db, staff_user):
    with pytest.raises(AdmissionNotFoundError):
        ward_engine.discharge_patient(db, admission_id=uuid.uuid4(), discharged_by=staff_user.id)


def test_discharge_patient_already_discharged(db, bed, patient, staff_user):
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )
    ward_engine.discharge_patient(db, admission_id=admission.id, discharged_by=staff_user.id)

    with pytest.raises(AlreadyDischargedError):
        ward_engine.discharge_patient(db, admission_id=admission.id, discharged_by=staff_user.id)


def test_discharge_patient_h1_guard_is_textually_inside_the_lock_against_refetched_row():
    """REVIEW-AGENT finding H1 (see ward_engine.discharge_patient's own
    docstring): the ORIGINAL bug checked `discharged_at is not None` once,
    before acquiring the lock, against a plain `db.get()` load. The fix
    re-checks entirely INSIDE the lock, against a `SELECT ... FOR
    UPDATE`-refetched row. This is a source-level pin confirming the fix's
    shape hasn't regressed back to a pre-lock check: every occurrence of the
    guard must appear textually AFTER both the lock acquisition and the
    `.with_for_update()` refetch."""
    source = _source_without_docstring(ward_engine.discharge_patient)
    assert "with_for_update()" in source

    lock_pos = source.index("lock_manager.acquire_all")
    refetch_pos = source.index("with_for_update()")
    guard_positions = [m.start() for m in re.finditer(r"discharged_at is not None", source)]
    assert guard_positions, "expected an in-lock 'already discharged' guard"
    assert all(pos > lock_pos and pos > refetch_pos for pos in guard_positions)


def test_discharge_patient_h1_regression_stale_identity_map_defeats_the_refetch(db, bed, patient, staff_user):
    """FINDING (Phase 3, confirmed via a standalone, single-threaded,
    deterministic repro -- not a timing-dependent guess): the H1 fix's
    SHAPE is correct (the previous test confirms the guard is textually
    after `with_for_update()`), but it does NOT actually close the race in
    practice, because of a SQLAlchemy identity-map subtlety:
    `discharge_patient` opens with an UNLOCKED pre-lock
    `admission = db.get(Admission, admission_id)` (needed only to learn
    which bed to lock before a lock can be requested). That call populates
    the calling `Session`'s identity map with an `Admission` instance. Later,
    INSIDE the lock, `select(Admission)...with_for_update()` DOES issue a
    real, row-locking SQL statement against Postgres -- but because an
    instance for this primary key already exists in the SAME session's
    identity map (from the pre-lock read) and has not been expired or
    reloaded with `populate_existing()`, SQLAlchemy's default behavior
    returns THAT SAME Python object without refreshing its attributes from
    the new result row. So `admission.discharged_at` inside the lock still
    reflects whatever it was at the moment of the PRE-LOCK read, not the
    genuinely current, row-locked value -- the exact TOCTOU H1 was supposed
    to close, reopened one layer down in the ORM.

    In production this matters for exactly the case H1 exists to cover:
    each HTTP request gets its OWN `Session` via `Depends(get_db)`, and each
    call to `discharge_patient` does its OWN pre-lock read on that session
    before acquiring the lock -- so two genuinely concurrent discharge/
    transfer requests for the same admission (two different sessions/
    connections) can both load `discharged_at=None` into their own session's
    identity map before either commits, and whichever one's session already
    holds that stale copy when it later reaches the in-lock refetch will
    NOT see the other's committed discharge, silently overwriting
    `stay_range`/`discharged_at` a second time with no error ever raised.

    This test reproduces that exact necessary condition directly (one
    session already holding a pre-lock-loaded `Admission` instance when a
    DIFFERENT session discharges the same admission and commits), with no
    dependence on OS thread-scheduling timing -- see the threaded test below
    for the same defect demonstrated under genuine concurrent load.

    Written the ordinary way round (`pytest.raises(AlreadyDischargedError)`,
    same convention as every other exception test in this file): it
    currently FAILS, because the guard does not actually raise. It will PASS
    once a real fix lands (e.g. `.execution_options(populate_existing=True)`
    on the in-lock refetch, or an explicit `db.expire(admission)`/
    `db.refresh(admission)` between the pre-lock read and the in-lock
    refetch) -- a red-to-green signal, not the other way round."""
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )
    admission_id = admission.id

    # A session that has ALREADY loaded this Admission into its identity map
    # -- simulating the moment right after discharge_patient's own pre-lock
    # `db.get()` line executes on its very first call.
    stale_session = TestSessionLocal()
    stale_read = stale_session.get(Admission, admission_id)
    assert stale_read.discharged_at is None

    # A genuinely DIFFERENT session/connection discharges the same
    # admission for real and commits.
    other_session = TestSessionLocal()
    try:
        ward_engine.discharge_patient(other_session, admission_id=admission_id, discharged_by=staff_user.id)
    finally:
        other_session.close()

    try:
        # `stale_session` now calls discharge_patient AGAIN. Its internal
        # pre-lock `db.get()` returns the ALREADY-cached (stale) instance
        # straight from the identity map (no DB round-trip at all), and the
        # in-lock `with_for_update()` refetch -- per the bug above -- also
        # returns that same stale instance rather than the fresh, row-locked
        # data, so `AlreadyDischargedError` is (incorrectly) never raised.
        with pytest.raises(AlreadyDischargedError):
            ward_engine.discharge_patient(stale_session, admission_id=admission_id, discharged_by=staff_user.id)
    finally:
        stale_session.close()


def test_discharge_patient_concurrent_double_discharge_exactly_one_wins(bed, patient, staff_user):
    """The real TOCTOU race H1 fixed: many near-simultaneous discharge calls
    for the SAME admission. Without the fix, two callers' pre-lock reads
    could both observe `discharged_at is None` before either commits, and
    the second would silently overwrite the first's close under the lock
    (no EXCLUDE-style backstop protects a second UPDATE the way one protects
    a second INSERT). With the fix, only the caller who wins the lock race
    AND passes the freshly re-fetched, row-locked check may succeed --
    everyone else gets a clean `AlreadyDischargedError`, never a silent
    double-close. Uses 6 threads (not 2) and a `Barrier` to maximize the
    chance every thread's unlocked pre-lock `db.get()` executes before any
    of them finishes the locked section, which is exactly the window the
    original bug needed.

    NOTE: unlike the deterministic test directly above (which reproduces the
    root cause -- SQLAlchemy identity-map staleness -- with no timing
    dependency), THIS test's outcome depends on real OS thread scheduling:
    it usually demonstrates multiple threads succeeding (the same bug, under
    genuine concurrent load), but can occasionally show exactly one winner
    "by luck" if a given run's scheduling happens to fully serialize the
    pre-lock reads behind a commit. A pass here on any single run is NOT
    proof the bug is fixed -- the deterministic test above is authoritative
    for that; this test is corroborating evidence under realistic
    conditions, not the primary regression signal."""
    setup_db = TestSessionLocal()
    try:
        admission = ward_engine.admit_patient(
            setup_db,
            patient_id=patient.id,
            bed_id=bed.id,
            start_time=datetime.now(timezone.utc) - timedelta(hours=1),
            admitted_by=staff_user.id,
        )
        admission_id, staff_id = admission.id, staff_user.id
    finally:
        setup_db.close()

    n_threads = 6
    barrier = threading.Barrier(n_threads)
    results: dict[str, object] = {}

    def _discharge(key: int) -> None:
        conn = TestSessionLocal()
        try:
            barrier.wait(timeout=5)
            updated = ward_engine.discharge_patient(conn, admission_id=admission_id, discharged_by=staff_id)
            results[f"{key}_ok"] = updated.discharged_at
        except AlreadyDischargedError as exc:
            results[f"{key}_already"] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=_discharge, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    successes = [k for k in results if k.endswith("_ok")]
    already = [k for k in results if k.endswith("_already")]
    assert len(successes) == 1, f"expected exactly one winner, got: {results}"
    assert len(already) == n_threads - 1, f"every loser must cleanly raise AlreadyDischargedError, got: {results}"

    verify_db = TestSessionLocal()
    try:
        final = _fresh_admission(verify_db, admission_id)
        assert final.discharged_at is not None
        assert final.stay_range.upper is not None  # closed exactly once, not corrupted
    finally:
        verify_db.close()


# ---------------------------------------------------------------------------
# transfer_patient
# ---------------------------------------------------------------------------


def test_transfer_patient_happy_path(db, bed, other_bed, patient, staff_user):
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )

    new_admission = ward_engine.transfer_patient(
        db, admission_id=admission.id, new_bed_id=other_bed.id, actor_user_id=staff_user.id
    )

    assert new_admission.bed_id == other_bed.id
    assert new_admission.patient_id == patient.id
    assert new_admission.discharged_at is None

    assert _fresh_bed_status(db, bed.id) == BedStatus.cleaning
    assert _fresh_bed_status(db, other_bed.id) == BedStatus.occupied

    old_admission = _fresh_admission(db, admission.id)
    assert old_admission.discharged_at is not None
    assert old_admission.stay_range.upper is not None


def test_transfer_patient_admission_not_found(db, other_bed, staff_user):
    with pytest.raises(AdmissionNotFoundError):
        ward_engine.transfer_patient(
            db, admission_id=uuid.uuid4(), new_bed_id=other_bed.id, actor_user_id=staff_user.id
        )


def test_transfer_patient_already_discharged(db, bed, other_bed, patient, staff_user):
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )
    ward_engine.discharge_patient(db, admission_id=admission.id, discharged_by=staff_user.id)

    with pytest.raises(AlreadyDischargedError):
        ward_engine.transfer_patient(
            db, admission_id=admission.id, new_bed_id=other_bed.id, actor_user_id=staff_user.id
        )


def test_transfer_patient_h1_regression_stale_identity_map_defeats_the_refetch(db, bed, other_bed, patient, staff_user):
    """Same root cause as
    test_discharge_patient_h1_regression_stale_identity_map_defeats_the_refetch,
    applied to `transfer_patient`'s own `discharged_at` guard, which has the
    IDENTICAL shape (unlocked pre-lock `db.get()`, then an in-lock
    `select(...).with_for_update()` re-check on the same `Session`). A
    session that already holds a pre-lock-loaded, not-yet-discharged
    `Admission` instance, when a DIFFERENT session discharges that admission
    in the meantime, will NOT see the discharge when it later calls
    `transfer_patient` -- it will proceed to (incorrectly) transfer an
    already-discharged stay instead of raising `AlreadyDischargedError`.
    Written the ordinary way round (`pytest.raises`): currently FAILS,
    will PASS once the same fix that closes the discharge_patient version
    of this bug lands here too."""
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )
    admission_id = admission.id

    stale_session = TestSessionLocal()
    stale_read = stale_session.get(Admission, admission_id)
    assert stale_read.discharged_at is None

    other_session = TestSessionLocal()
    try:
        ward_engine.discharge_patient(other_session, admission_id=admission_id, discharged_by=staff_user.id)
    finally:
        other_session.close()

    try:
        with pytest.raises(AlreadyDischargedError):
            ward_engine.transfer_patient(
                stale_session, admission_id=admission_id, new_bed_id=other_bed.id, actor_user_id=staff_user.id
            )
    finally:
        stale_session.close()


def test_transfer_patient_h2_new_bed_not_available(db, bed, other_bed, patient, staff_user):
    """REVIEW-AGENT finding H2 (see transfer_patient's own docstring): a
    `blocked`/`cleaning` new bed has NO active admission row at all, so the
    EXCLUDE constraint alone would never reject a transfer landing a patient
    in one -- this fast-path status check is the only thing that does.
    Also confirms atomicity: the rejection must happen before ANY mutation,
    so the old admission/bed are left completely untouched."""
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )

    other_bed_row = db.get(Bed, other_bed.id)
    other_bed_row.status = BedStatus.blocked
    db.add(other_bed_row)
    db.commit()

    with pytest.raises(BedNotAvailableError):
        ward_engine.transfer_patient(
            db, admission_id=admission.id, new_bed_id=other_bed.id, actor_user_id=staff_user.id
        )

    old_admission = _fresh_admission(db, admission.id)
    assert old_admission.discharged_at is None
    assert _fresh_bed_status(db, bed.id) == BedStatus.occupied
    assert _fresh_bed_status(db, other_bed.id) == BedStatus.blocked


def test_transfer_patient_bed_conflict_via_exclude_constraint_rolls_back_atomically(
    db, bed, other_bed, patient, staff_user
):
    """Same drift trick as admit_patient's EXCLUDE-conflict test, applied to
    the new bed: an active admission already occupies `other_bed`, but its
    status is reset to `available` so the fast-path (H2) check passes and
    only the EXCLUDE constraint catches the real overlap on insert. Confirms
    the PRP's atomicity requirement directly: "a transfer that fails to free
    the old bed AND grab the new one is worse than one that does neither" --
    the old admission/bed must be left EXACTLY as they were, never "stuck
    cleaning with no matching admission"."""
    admission = ward_engine.admit_patient(
        db,
        patient_id=patient.id,
        bed_id=bed.id,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        admitted_by=staff_user.id,
    )

    second_patient = _make_patient(db)
    ward_engine.admit_patient(
        db,
        patient_id=second_patient.id,
        bed_id=other_bed.id,
        start_time=datetime.now(timezone.utc),
        admitted_by=staff_user.id,
    )
    drifted = db.get(Bed, other_bed.id)
    drifted.status = BedStatus.available
    db.add(drifted)
    db.commit()

    with pytest.raises(BedConflictError):
        ward_engine.transfer_patient(
            db, admission_id=admission.id, new_bed_id=other_bed.id, actor_user_id=staff_user.id
        )

    old_admission = _fresh_admission(db, admission.id)
    assert old_admission.discharged_at is None, "old admission must not have been closed -- no orphaned state"
    assert _fresh_bed_status(db, bed.id) == BedStatus.occupied, "old bed must not be stuck in cleaning"


def test_transfer_patient_acquires_both_bed_locks_in_a_single_call():
    """REVIEW-AGENT's specific ask (Phase 3): both bed keys MUST be passed to
    ONE `lock_manager.acquire_all([...])` call, never two sequential
    single-key acquisitions -- that would defeat the lock manager's sorted-
    ordering deadlock prevention (core/locking.py)."""
    source = _source_without_docstring(ward_engine.transfer_patient)
    assert source.count("lock_manager.acquire_all") == 1

    call_match = re.search(r"lock_manager\.acquire_all\((\[[^\]]*\])\)", source)
    assert call_match is not None
    call_args = call_match.group(1)
    assert "old_bed_id" in call_args
    assert "new_bed_id" in call_args


# ---------------------------------------------------------------------------
# set_bed_status
# ---------------------------------------------------------------------------

_ALL_BED_STATUSES = list(BedStatus)
_LEGAL_TRANSITIONS = {
    (BedStatus.cleaning, BedStatus.available),
    (BedStatus.available, BedStatus.blocked),
    (BedStatus.cleaning, BedStatus.blocked),
    (BedStatus.blocked, BedStatus.available),
}
_ALL_PAIRS = set(itertools.product(_ALL_BED_STATUSES, _ALL_BED_STATUSES))
_ILLEGAL_TRANSITIONS = _ALL_PAIRS - _LEGAL_TRANSITIONS


def _bed_with_status(db, ward, status: BedStatus) -> Bed:
    b = Bed(ward_id=ward.id, label=f"Bed-{uuid.uuid4().hex[:8]}", status=status)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.mark.parametrize("current,requested", sorted(_LEGAL_TRANSITIONS, key=str))
def test_set_bed_status_legal_transitions_succeed(db, ward, staff_user, current, requested):
    b = _bed_with_status(db, ward, current)

    updated = ward_engine.set_bed_status(db, bed_id=b.id, requested_status=requested, actor_user_id=staff_user.id)

    assert updated.status == requested
    assert _fresh_bed_status(db, b.id) == requested


@pytest.mark.parametrize("current,requested", sorted(_ILLEGAL_TRANSITIONS, key=str))
def test_set_bed_status_illegal_transitions_raise_with_correct_current_and_requested(
    db, ward, staff_user, current, requested
):
    """Covers, by construction, every occupied-as-source and
    occupied-as-target pair -- `occupied` never appears in
    `_LEGAL_TRANSITIONS`, so it is automatically included here (see
    ward_engine.py / models/ward.py: occupied is only ever set by
    `admit_patient` / cleared by `discharge_patient`, never by this
    function)."""
    b = _bed_with_status(db, ward, current)

    with pytest.raises(IllegalBedStatusTransition) as exc_info:
        ward_engine.set_bed_status(db, bed_id=b.id, requested_status=requested, actor_user_id=staff_user.id)

    assert exc_info.value.current == current
    assert exc_info.value.requested == requested
    # illegal attempt must not mutate anything.
    assert _fresh_bed_status(db, b.id) == current


def test_set_bed_status_bed_not_found(db, staff_user):
    with pytest.raises(BedNotFoundError):
        ward_engine.set_bed_status(
            db, bed_id=uuid.uuid4(), requested_status=BedStatus.available, actor_user_id=staff_user.id
        )


# ---------------------------------------------------------------------------
# schedule_ot
# ---------------------------------------------------------------------------


def test_schedule_ot_happy_path(db, ot_room, patient, doctor_record, staff_user):
    start = datetime.now(timezone.utc) + timedelta(hours=1)

    ot = ward_engine.schedule_ot(
        db,
        room_id=ot_room.id,
        patient_id=patient.id,
        surgeon_id=doctor_record.id,
        start_time=start,
        duration_minutes=60,
        actor_user_id=staff_user.id,
    )

    assert ot.room_id == ot_room.id
    assert ot.surgeon_id == doctor_record.id
    assert ot.patient_id == patient.id

    audit_row = db.execute(
        select(AuditLog).where(AuditLog.action == "ward.ot_scheduled", AuditLog.resource_id == str(ot.id))
    ).scalar_one()
    assert audit_row.event_metadata["room_id"] == str(ot_room.id)
    assert audit_row.event_metadata["surgeon_id"] == str(doctor_record.id)


def test_schedule_ot_room_conflict_different_surgeons_raises_room_conflict(
    db, ot_room, patient, doctor_record, other_doctor_record, staff_user
):
    """The room-only EXCLUDE constraint is the authoritative guard here --
    proven by using TWO DIFFERENT surgeons so the app-level surgeon
    pre-check cannot possibly be what trips."""
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    ward_engine.schedule_ot(
        db,
        room_id=ot_room.id,
        patient_id=patient.id,
        surgeon_id=doctor_record.id,
        start_time=start,
        duration_minutes=60,
        actor_user_id=staff_user.id,
    )

    overlapping_start = start + timedelta(minutes=30)
    with pytest.raises(OTConflictError) as exc_info:
        ward_engine.schedule_ot(
            db,
            room_id=ot_room.id,
            patient_id=patient.id,
            surgeon_id=other_doctor_record.id,
            start_time=overlapping_start,
            duration_minutes=60,
            actor_user_id=staff_user.id,
        )
    assert exc_info.value.reason == "room_conflict"


def test_schedule_ot_surgeon_busy_in_another_ot_raises_surgeon_busy_ot(
    db, ot_room, other_ot_room, patient, doctor_record, staff_user
):
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    ward_engine.schedule_ot(
        db,
        room_id=ot_room.id,
        patient_id=patient.id,
        surgeon_id=doctor_record.id,
        start_time=start,
        duration_minutes=60,
        actor_user_id=staff_user.id,
    )

    overlapping_start = start + timedelta(minutes=30)
    with pytest.raises(OTConflictError) as exc_info:
        ward_engine.schedule_ot(
            db,
            room_id=other_ot_room.id,  # different room -- the room EXCLUDE constraint cannot catch this
            patient_id=patient.id,
            surgeon_id=doctor_record.id,  # same surgeon
            start_time=overlapping_start,
            duration_minutes=60,
            actor_user_id=staff_user.id,
        )
    assert exc_info.value.reason == "surgeon_busy_ot"


def test_schedule_ot_surgeon_busy_against_clinic_appointment_raises_surgeon_busy_appointment(
    db, ot_room, room, branch, patient, doctor_record, staff_user
):
    """Proves the app-level surgeon-availability check actually queries
    `appointments`, not just `ot_schedules` -- design decision #3's
    documented cross-table gap this check exists specifically to cover
    (PRP Phase 3: "write a test that specifically proves the surgeon check
    catches a conflict against a regular clinic appointments row, not just
    another OT slot")."""
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    end = start + timedelta(minutes=30)
    appt = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add(appt)
    db.commit()

    overlapping_start = start + timedelta(minutes=10)
    with pytest.raises(OTConflictError) as exc_info:
        ward_engine.schedule_ot(
            db,
            room_id=ot_room.id,
            patient_id=patient.id,
            surgeon_id=doctor_record.id,
            start_time=overlapping_start,
            duration_minutes=30,
            actor_user_id=staff_user.id,
        )
    assert exc_info.value.reason == "surgeon_busy_appointment"


def test_schedule_ot_inactive_clinic_appointment_does_not_block(db, ot_room, room, branch, patient, doctor_record, staff_user):
    """A cancelled clinic appointment must NOT count as a conflict -- same
    `_INACTIVE_APPOINTMENT_STATUSES` filter `scheduling_engine.py` uses for
    its own doctor-conflict check."""
    start = datetime.now(timezone.utc) + timedelta(hours=5)
    end = start + timedelta(minutes=30)
    appt = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.cancelled,
    )
    db.add(appt)
    db.commit()

    ot = ward_engine.schedule_ot(
        db,
        room_id=ot_room.id,
        patient_id=patient.id,
        surgeon_id=doctor_record.id,
        start_time=start,
        duration_minutes=30,
        actor_user_id=staff_user.id,
    )
    assert ot.surgeon_id == doctor_record.id


# ---------------------------------------------------------------------------
# FINDING: lock-contention exception is never translated to ResourceBusyError
# ---------------------------------------------------------------------------


def test_lock_contention_propagates_as_ResourceBusyError(
    monkeypatch, db, bed, patient, staff_user
):
    """Every function in `ward_engine.py` now wraps its lock scope in
    `try/except LockAcquisitionError: raise ResourceBusyError`, matching
    `scheduling_engine.book_appointment`'s pattern -- fixed after this test
    originally pinned the opposite (a raw `LockAcquisitionError` leaking
    past `routers/wards.py`'s `except ResourceBusyError: -> 423` handler,
    which would have produced an unhandled 500). See
    test_wards_router.py's matching router-level test for the HTTP-visible
    consequence, now also updated to assert 423."""

    def _raise(*_args, **_kwargs):
        raise LockAcquisitionError("simulated lock contention")

    monkeypatch.setattr(ward_engine.lock_manager, "acquire_all", _raise)

    with pytest.raises(ward_engine.ResourceBusyError):
        ward_engine.admit_patient(
            db,
            patient_id=patient.id,
            bed_id=bed.id,
            start_time=datetime.now(timezone.utc),
            admitted_by=staff_user.id,
        )
