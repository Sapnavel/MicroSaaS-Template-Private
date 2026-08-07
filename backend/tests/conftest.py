"""Shared pytest fixtures for the auth/RBAC/ABAC test suite.

INFRA NOTE (read before touching this file): this schema uses Postgres-native
features (native ENUM types, UUID, TSTZRANGE, EXCLUDE USING gist constraints
on other modules' tables that share `Base.metadata`) so SQLite cannot run
`Base.metadata.create_all()` for this project — and importing `app.main` /
`app.database` transitively imports `app.models`, which registers every
model, not just the auth ones. Tests therefore run against a **real**
Postgres + Redis, brought up via `docker compose up -d postgres redis`
(see PRPs/auth-rbac-abac-prp.md Phase 3 for the exact commands). The schema
itself (`database/schema.sql`) is applied by that container's init-script
mount, NOT by SQLAlchemy — so this file never calls
`Base.metadata.create_all()`/`drop_all()`; it truncates the relevant tables
between tests instead.

Test isolation choices (documented per the task brief):
- Postgres: `db` fixture truncates `refresh_tokens`, `users`, `branches`,
  `hospital_groups` (CASCADE) before every test, so each test starts from an
  empty table set rather than relying on unique emails alone.
- Redis: fixture flushes the **same logical DB the app uses**
  (`REDIS_URL=redis://localhost:6379/0`) before every test. A separate
  logical DB (e.g. `/1`) was considered for isolation, but
  `core/security.py` and `core/rate_limit.py` both build their Redis client
  from `settings.redis_url` at import time, so pointing tests at a different
  DB index than the app would require re-pointing those already-constructed
  clients too. Flushing `/0` before each test is simpler and safe here
  because nothing else uses this Postgres/Redis pair while tests run.
"""

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault(
    "JWT_SIGNING_KEYS",
    '{"k1": "test-signing-key-one-yyyyyyyyyyyyyyyyyyyy", '
    '"k2": "test-signing-key-two-xxxxxxxxxxxxxxxxxxxx"}',
)
os.environ.setdefault("JWT_CURRENT_KID", "k2")
os.environ.setdefault("HMAC_KEY", "test-hmac-key-not-shared-with-jwt")
os.environ.setdefault(
    "PHI_ENCRYPTION_KEY", "ZJqygQE1MBAqt8CWi7RygP4VAyWSWMt6iEQzB6XIhmQ="
)
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://hms:hms@localhost:5432/hms")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("LOGIN_RATE_LIMIT_PER_MINUTE", "3")

# Settings is constructed at import time in app.config (`settings = get_settings()`)
# and cached via @lru_cache — clear the cache now, before anything imports
# app.config/app.main, so the env vars above are the ones baked into the
# singleton every other module imports.
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings as app_settings  # noqa: E402
from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.appointment import Appointment, AppointmentStatus  # noqa: E402
from app.models.consultation import Consultation, Drug  # noqa: E402
from app.models.patient import Patient  # noqa: E402
from app.models.resource import Doctor, Room, Specialty  # noqa: E402
from app.models.tenant import Branch, HospitalGroup  # noqa: E402
from app.models.user import UserRole  # noqa: E402
from app.models.ward import Bed, BedStatus, Ward  # noqa: E402
from app.services import auth_service  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
TEST_REDIS_URL = os.environ["REDIS_URL"]

engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# database/seed_clinical_reference_data.sql is deliberately NOT applied by
# docker-entrypoint-initdb.d (only schema.sql is -- see that seed file's own
# header) and `drugs`/`drug_interactions` are deliberately NOT in
# TRUNCATE_TABLES below (they're seeded reference data, not per-test state).
# But nothing else ever loads that file into the test database either -- on
# a freshly created Postgres volume both tables are simply empty, which is
# exactly why test_prescription_safety.py/test_consultations.py's drug-name
# lookups (`_drug_id(db, "Coumadin")`, etc.) raised `NoResultFound` before
# this fixture existed. Applying it once per test session closes that gap.
#
# The presence check below deliberately looks up a SPECIFIC seeded drug name
# ("Coumadin"), NOT `SELECT COUNT(*) FROM drugs` -- the Pharmacy module's own
# `drug`/`other_drug` fixtures (see below) insert their own rows into this
# same, never-truncated table with random `Test Drug <hex>` names, so a
# plain count is already nonzero on any dev database that has ever run a
# pharmacy test. Checking for one real seeded name is the only reliable way
# to tell "has the reference data actually been loaded" from "some other
# module's fixtures have left unrelated rows behind".
_SEED_FILE = Path(__file__).resolve().parent.parent.parent / "database" / "seed_clinical_reference_data.sql"


@pytest.fixture(scope="session", autouse=True)
def _seed_clinical_reference_data():
    with engine.begin() as conn:
        already_seeded = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM drugs WHERE name = 'Coumadin')")
        ).scalar_one()
        if not already_seeded:
            conn.execute(text(_SEED_FILE.read_text(encoding="utf-8")))

# Extended by PRPs/patient-master-index-prp.md's TEST-AGENT (Phase 3) to
# cover the Patient Master Index module's tables. `patients` CASCADEs into
# every table with a `patient_id` FK (appointments, consultations, etc. --
# see the NOTICE list from a manual `TRUNCATE ... CASCADE` against
# schema.sql), and `users` CASCADEs into `doctors`; `specialties`/`rooms`
# have no FK back to `users`/`patients` so they're listed explicitly.
# `audit_logs` is listed explicitly too since several of this module's tests
# assert on exact audit-log row counts/content and would otherwise leak rows
# across tests.
#
# Extended by PRPs/clinical-consultation-prescription-prp.md's TEST-AGENT
# (Phase 3) with `consultations, diagnoses, patient_allergies, prescriptions,
# prescription_items` -- `patients`/`appointments` already CASCADE into most
# of these (consultations.patient_id/appointment_id, prescriptions.patient_id,
# patient_allergies.patient_id all FK back to already-truncated tables), but
# listing them explicitly keeps this list self-documenting and robust against
# a future FK path that doesn't happen to route through `patients`/
# `appointments`. Deliberately NOT truncating `drugs`/`drug_interactions`:
# those are seeded reference data (database/seed_clinical_reference_data.sql),
# not per-test state -- see that file's header for why it's applied once,
# manually, outside the docker-entrypoint-initdb.d mechanism.
#
# Extended by PRPs/lab-module-prp.md's TEST-AGENT (Phase 3) with
# `lab_orders, lab_samples` -- `consultations`/`patients` already CASCADE
# into most FK paths, but listing these two explicitly keeps the list
# self-documenting (same rationale as the Consultation module's addition
# above) and matters here specifically because several lab tests assert on
# exact `audit_logs` row content/counts and on `lab_samples` actor/timestamp
# columns directly, which would leak across tests otherwise.
#
# Extended by PRPs/pharmacy-module-prp.md's TEST-AGENT (Phase 3) with
# `inventory_items, inventory_batches` -- neither CASCADEs in from any table
# already in this list (`inventory_items.branch_id` FKs to `branches`, which
# IS truncated, so CASCADE would eventually reach them, but listing both
# explicitly keeps this list self-documenting, same rationale as every prior
# module's addition above, and matters concretely here because several
# pharmacy tests assert on exact `inventory_items`/`inventory_batches` row
# counts and quantities directly). Deliberately NOT truncating `drugs` --
# same "seeded reference data, not per-test state" rule the Consultation
# module's addition above already established.
#
# Extended by PRPs/ward-bed-ot-module-prp.md's TEST-AGENT (Phase 3) with
# `wards, beds, admissions, ot_schedules` -- `wards.branch_id` FKs to
# `branches` (already truncated, so CASCADE would eventually reach the whole
# chain wards -> beds -> admissions, and `ot_schedules.room_id` FKs to
# `rooms`, also already truncated), but listing all four explicitly keeps
# this list self-documenting (same rationale as every prior module's
# addition above) and matters concretely here because several ward tests
# assert on exact `Bed.status`/`Admission.discharged_at`/`stay_range` state
# directly, which would leak across tests otherwise.
#
# Extended by PRPs/billing-module-prp.md's TEST-AGENT (Phase 3) with
# `invoices, invoice_items, insurance_claims` -- `invoices.patient_id` FKs to
# `patients` (already truncated, so CASCADE would eventually reach the whole
# chain invoices -> invoice_items/insurance_claims), but listing all three
# explicitly keeps this list self-documenting (same rationale as every prior
# module's addition above) and matters concretely here because the
# `invoice_items_source_unique` duplicate-charge tests assert on exact row
# counts/constraint behavior that would leak across tests otherwise.
TRUNCATE_TABLES = (
    "refresh_tokens, users, branches, hospital_groups, "
    "patients, patient_duplicate_candidates, appointments, doctors, rooms, specialties, audit_logs, "
    "consultations, diagnoses, patient_allergies, prescriptions, prescription_items, "
    "lab_orders, lab_samples, inventory_items, inventory_batches, "
    "wards, beds, admissions, ot_schedules, "
    "invoices, invoice_items, insurance_claims"
)


@pytest.fixture
def db():
    """Session against the live test Postgres. Truncates the auth-relevant
    tables before AND after each test (before, in case a previous run was
    interrupted mid-test and left rows behind; after, to leave the shared
    dev sandbox clean)."""
    session = TestSessionLocal()
    session.execute(text(f"TRUNCATE TABLE {TRUNCATE_TABLES} CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text(f"TRUNCATE TABLE {TRUNCATE_TABLES} CASCADE"))
        session.commit()
        session.close()


@pytest.fixture
def redis_client(db):  # noqa: ARG001 - depends on db to order after table truncation
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture
def client(db, redis_client):  # noqa: ARG001 - redis_client ensures flush ordering
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# FINDING (fixed as part of Phase 3, see database/schema.sql): the ORM
# models `app.models.tenant.HospitalGroup`/`Branch` mix in `TimestampMixin`
# (created_at + updated_at), but `database/schema.sql` originally only
# defined `created_at` for these two tables. That mismatch broke every ORM
# query against them -- SELECT included, not just INSERT -- which in turn
# broke real request paths, not just these fixtures: `/auth/me` and login
# both call `resolve_hospital_group_id()` -> `db.get(Branch, ...)`, and
# `POST /admin/staff` looks up the branch the same way. Added the missing
# `updated_at` column to `database/schema.sql` (additive, matches what the
# ORM models already expected) rather than routing around it here, since
# working around it in fixtures alone would leave the app itself broken.


@pytest.fixture
def hospital_group(db) -> HospitalGroup:
    group = HospitalGroup(name=f"Test Group {uuid.uuid4().hex[:8]}")
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@pytest.fixture
def branch(db, hospital_group) -> Branch:
    b = Branch(hospital_group_id=hospital_group.id, name="Main Branch", timezone="UTC")
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def other_branch(db) -> Branch:
    """A branch under a DIFFERENT hospital group than `branch`, for the
    cross-branch ABAC tests."""
    other_group = HospitalGroup(name=f"Other Group {uuid.uuid4().hex[:8]}")
    db.add(other_group)
    db.commit()
    db.refresh(other_group)

    b = Branch(hospital_group_id=other_group.id, name="Other Branch", timezone="UTC")
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def staff_password() -> str:
    return "correct-horse-battery-staple"


@pytest.fixture
def staff_user(db, branch, staff_password):
    """Provisioned via the real service function (auth_service.provision_staff)
    so the test exercises actual application code, not a hand-rolled insert."""
    email = unique_email("doctor")
    user = auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Dr. Staff Member",
        role=UserRole.doctor.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )
    return user


@pytest.fixture
def patient_password() -> str:
    return "another-strong-password-1"


@pytest.fixture
def patient_user(db, patient_password):
    """Registered via the real service function (auth_service.register_patient)."""
    email = unique_email("patient")
    user = auth_service.register_patient(db, email, patient_password, "Patient Person")
    return user


@pytest.fixture
def system_admin_user(db, branch, staff_password):
    """system_admin is cross-branch by role, not by a NULL branch_id:
    `StaffProvisionRequest.branch_id` / `provision_staff(branch_id=...)` are
    non-optional (the schema requires a real, existing branch row), and
    `authorize()`'s tenant-guard bypass keys off `user.role ==
    UserRole.system_admin` alone, not off `branch_id` being None. So this
    fixture provisions the admin under a real branch like any other staff
    account; the login endpoint's `branch_id is None` check is only ever
    reached for non-admin roles anyway (see routers/auth.py `login`)."""
    email = unique_email("sysadmin")
    user = auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="System Admin",
        role=UserRole.system_admin.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )
    return user


# ---------------------------------------------------------------------------
# PRPs/patient-master-index-prp.md Phase 3 additions. `staff_user` above is
# already role=doctor (see its docstring) -- reused as-is by
# test_patients.py wherever a plain "doctor" caller is needed, rather than
# adding a redundant duplicate fixture. `front_desk_user` / `nurse_user` are
# genuinely new roles this module's tests need that no earlier module
# exercised through the API.
# ---------------------------------------------------------------------------


@pytest.fixture
def front_desk_user(db, branch, staff_password):
    email = unique_email("frontdesk")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Front Desk Clerk",
        role=UserRole.front_desk.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )


@pytest.fixture
def nurse_user(db, branch, staff_password):
    email = unique_email("nurse")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Nurse Nightingale",
        role=UserRole.nurse.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )


@pytest.fixture
def specialty(db) -> Specialty:
    sp = Specialty(name=f"Specialty-{uuid.uuid4().hex[:8]}")
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


@pytest.fixture
def room(db, branch) -> Room:
    r = Room(branch_id=branch.id, name="Room 101", room_type="consultation")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@pytest.fixture
def doctor_record(db, branch, staff_user, specialty) -> Doctor:
    """A `Doctor` (scheduling-resource) row, distinct from `staff_user` (the
    `User` row with role=doctor) -- `Doctor.user_id` links the two, same as
    real staff onboarding would. Needed for the merge-reassignment test's
    `Appointment` fixture chain (Appointment requires a real doctor_id)."""
    doc = Doctor(user_id=staff_user.id, branch_id=branch.id, specialty_id=specialty.id)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# PRPs/clinical-consultation-prescription-prp.md Phase 3 additions.
#
# `staff_user`/`doctor_record` above already give a correctly-wired
# doctor (a `User` with role=doctor paired with a `Doctor` row via
# `Doctor.user_id == User.id`) -- reused as-is here rather than building a
# parallel doctor fixture, per this TEST-AGENT's brief (a `Doctor.user_id`
# pairing that ISN'T wired correctly is exactly the kind of fixture bug that
# would make a legitimate "doctor reads their own consultation" test 403
# spuriously, looking like a real ownership-policy bug when it's actually a
# broken fixture -- see core/security.py's `_caller_doctor_id` /
# `get_current_user` docstrings for the real bug this was fixed from).
#
# `other_doctor_user`/`other_doctor_record` are a SECOND, independently-wired
# doctor -- needed for every "a different doctor must be denied" test in
# test_consultations.py (start-on-someone-else's-appointment, read/complete/
# diagnose/prescribe someone else's consultation).
# ---------------------------------------------------------------------------


@pytest.fixture
def other_doctor_user(db, branch, staff_password):
    email = unique_email("otherdoctor")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Dr. Other Doctor",
        role=UserRole.doctor.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )


@pytest.fixture
def other_doctor_record(db, branch, other_doctor_user, specialty) -> Doctor:
    doc = Doctor(user_id=other_doctor_user.id, branch_id=branch.id, specialty_id=specialty.id)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.fixture
def patient(db) -> Patient:
    """A minimal `Patient` (Patient Master Index) row, built directly via
    the ORM rather than through the Patient module's API -- this module's
    tests only need a valid `patient_id` to hang consultations/allergies/
    prescriptions off of, not the dedup-matching machinery."""
    p = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:10]}",
        full_name="Clinical Test Patient",
        dob=date(1985, 6, 15),
        sex="F",
        phone=f"555-{uuid.uuid4().hex[:7]}",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture
def in_progress_appointment(db, branch, doctor_record, room, patient) -> Appointment:
    """An `Appointment` already `in_progress` for `doctor_record`/`patient`
    -- the one precondition `consultation_service.start_consultation`
    requires (per the PRP's ENDPOINTS table: 400 if the appointment isn't
    `in_progress`)."""
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    appointment = Appointment(
        branch_id=branch.id,
        patient_id=patient.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    return appointment


@pytest.fixture
def consultation(db, in_progress_appointment) -> Consultation:
    """A `Consultation` already started from `in_progress_appointment`,
    built directly via the ORM (not via `POST /api/v1/consultations`) so
    tests that only care about diagnoses/allergies/prescriptions don't need
    to re-exercise the start-consultation endpoint every time. `doctor_id`/
    `patient_id` are copied from the appointment, exactly as
    `consultation_service.start_consultation` does it."""
    c = Consultation(
        appointment_id=in_progress_appointment.id,
        doctor_id=in_progress_appointment.doctor_id,
        patient_id=in_progress_appointment.patient_id,
        symptoms="Fatigue and mild fever",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# PRPs/lab-module-prp.md Phase 3 additions. `staff_user`/`nurse_user`/
# `other_doctor_user`/`consultation` above already cover everything the Lab
# module's tests need except a role=lab_tech caller, which no prior module
# exercised through the API -- lab_tech is the only role with transition
# rights over EVERY step of the state machine (see
# services/lab_service.py's `_TRANSITION_ROLES`), so test_lab.py needs a
# real one to walk a full ordered->attached chain.
# ---------------------------------------------------------------------------


@pytest.fixture
def lab_tech_user(db, branch, staff_password):
    email = unique_email("labtech")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Lab Tech Larry",
        role=UserRole.lab_tech.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# PRPs/pharmacy-module-prp.md Phase 3 additions. `branch`/`other_branch`
# above already give two independently-wired branches -- `other_branch` was
# added by the Patient Master Index TEST-AGENT for its own cross-branch ABAC
# tests but is reused as-is here, since this is the first module besides
# that one where a second branch actually matters (see this module's PRP:
# Patient/Consultation/Lab are all deliberately branch-agnostic; Pharmacy is
# the first module in the build sequence where the tenant guard in
# `core/security.py`'s `authorize()` actually restricts access for real).
# `pharmacist_user` is a genuinely new role no prior module's TEST-AGENT
# needed to exercise through the API.
# ---------------------------------------------------------------------------


@pytest.fixture
def pharmacist_user(db, branch, staff_password):
    email = unique_email("pharmacist")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Pharmacist Patty",
        role=UserRole.pharmacist.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )


@pytest.fixture
def other_branch_pharmacist_user(db, other_branch, staff_password):
    """A SECOND, independently-branched pharmacist -- needed for the
    branch-isolation tests (a pharmacist at `other_branch` receiving stock
    there, so `branch`'s pharmacist can be proven unable to see/dispense
    against it)."""
    email = unique_email("otherpharmacist")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Pharmacist Percy",
        role=UserRole.pharmacist.value,
        branch_id=other_branch.id,
        provisioned_by=uuid.uuid4(),
    )


@pytest.fixture
def drug(db) -> Drug:
    """A minimal `Drug` reference row, built directly via the ORM -- this
    module's tests only need a valid `drug_id` to hang inventory items/
    batches off of, not the seeded clinical reference dataset
    (`database/seed_clinical_reference_data.sql`) the Consultation/Lab
    modules' safety-checker tests depend on. A fresh row (not a seeded one)
    keeps this fixture independent of whether that seed script has been
    applied to a given test environment."""
    d = Drug(name=f"Test Drug {uuid.uuid4().hex[:8]}", generic_name="testadrine")
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


@pytest.fixture
def other_drug(db) -> Drug:
    """A second, distinct drug -- needed wherever a test must prove a
    dispense/query is scoped to a specific `drug_id`, not just any drug."""
    d = Drug(name=f"Other Test Drug {uuid.uuid4().hex[:8]}", generic_name="othertestadrine")
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


# ---------------------------------------------------------------------------
# PRPs/ward-bed-ot-module-prp.md Phase 3 additions. `branch`/`other_branch`,
# `nurse_user`/`front_desk_user`, `staff_user`/`doctor_record` (a "doctor"
# role caller wired to a real `Doctor` row -- reused here as a surgeon),
# `other_doctor_user`/`other_doctor_record` (a SECOND, independently-wired
# doctor -- reused here as a second surgeon for the OT surgeon-conflict
# tests), `room`/`patient`/`system_admin_user` above already cover everything
# this module's tests need except the Ward/Bed/OT-specific rows themselves
# (`Ward`, `Bed`) and a `room_type='ot'` room (the plain `room` fixture is
# `room_type='consultation'`, needed as-is for the surgeon-busy-appointment
# test, which deliberately books a regular clinic appointment, not an OT
# slot).
# ---------------------------------------------------------------------------


@pytest.fixture
def ward(db, branch) -> Ward:
    w = Ward(branch_id=branch.id, name="ICU", ward_type="icu")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


@pytest.fixture
def other_branch_ward(db, other_branch) -> Ward:
    """A second, independently-branched ward -- needed for the cross-branch
    ABAC tests (a nurse/doctor at `branch` must never admit/discharge/
    transfer/status-update a bed under `other_branch`'s ward)."""
    w = Ward(branch_id=other_branch.id, name="Other ICU", ward_type="icu")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


@pytest.fixture
def bed(db, ward) -> Bed:
    b = Bed(ward_id=ward.id, label=f"Bed-{uuid.uuid4().hex[:8]}", status=BedStatus.available)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def other_bed(db, ward) -> Bed:
    """A second bed in the SAME ward as `bed` -- needed for transfer tests
    (transfer moves a patient from `bed` to `other_bed`)."""
    b = Bed(ward_id=ward.id, label=f"Bed-{uuid.uuid4().hex[:8]}", status=BedStatus.available)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def other_branch_bed(db, other_branch_ward) -> Bed:
    b = Bed(ward_id=other_branch_ward.id, label=f"Bed-{uuid.uuid4().hex[:8]}", status=BedStatus.available)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def ot_room(db, branch) -> Room:
    """A `room_type='ot'` room -- distinct from the plain `room` fixture
    (`room_type='consultation'`), matching schema.sql's comment that OT
    scheduling targets rooms with `room_type='ot'`."""
    r = Room(branch_id=branch.id, name="OT-1", room_type="ot")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@pytest.fixture
def billing_admin_user(db, branch, staff_password):
    """PRPs/billing-module-prp.md Phase 3 addition. `front_desk_user` above
    is reused as-is for this module's read-only-role tests; `billing_admin`
    is a genuinely new role no prior module's TEST-AGENT exercised through
    the API."""
    email = unique_email("billingadmin")
    return auth_service.provision_staff(
        db,
        email=email,
        password=staff_password,
        full_name="Billing Admin Betty",
        role=UserRole.billing_admin.value,
        branch_id=branch.id,
        provisioned_by=uuid.uuid4(),
    )


@pytest.fixture
def other_ot_room(db, branch) -> Room:
    """A second OT room in the SAME branch -- needed for the surgeon-busy-in-
    another-OT conflict test (same surgeon, two different rooms, overlapping
    time)."""
    r = Room(branch_id=branch.id, name="OT-2", room_type="ot")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r
