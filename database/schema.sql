-- ============================================================================
-- Hospital Management & Appointment Booking System — Core DDL
-- PostgreSQL 15+
-- Requires extensions: pgcrypto (uuid), btree_gist (exclusion constraints on
-- non-range equality columns alongside range overlap), pg_trgm (trigram
-- similarity — fuzzy full_name matching for Patient Master Index dedup,
-- PRPs/patient-master-index-prp.md)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;
-- Added by BACKEND-AGENT, PRPs/patient-master-index-prp.md Phase 1: backs
-- the fuzzy full_name similarity() scan in services/patient_matching.py.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- 0. TENANCY
-- ============================================================================

-- NOTE (found by TEST-AGENT, PRPs/auth-rbac-abac-prp.md Phase 3): both
-- tables below include `updated_at` because app/models/tenant.py's
-- HospitalGroup/Branch ORM models mix in TimestampMixin (created_at +
-- updated_at). Without this column, any SQLAlchemy query against
-- hospital_groups/branches (SELECT included, not just INSERT) fails with
-- `UndefinedColumn: ... updated_at does not exist` -- this broke real
-- request paths, not just tests: /auth/me and login both call
-- resolve_hospital_group_id() -> db.get(Branch, ...), and POST /admin/staff
-- looks up the branch the same way. Added here to match the ORM models
-- rather than removing the mixin, since other Phase-1/2 code may already
-- assume Branch has updated_at.
CREATE TABLE hospital_groups (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE branches (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hospital_group_id   UUID NOT NULL REFERENCES hospital_groups(id),
    name                TEXT NOT NULL,
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    address             TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 1. AUTH / RBAC
-- ============================================================================

CREATE TYPE user_role AS ENUM (
    'patient', 'doctor', 'nurse', 'front_desk', 'lab_tech',
    'pharmacist', 'billing_admin', 'system_admin'
);

CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id           UUID REFERENCES branches(id),  -- NULL for system_admin (cross-branch)
    email               TEXT NOT NULL UNIQUE,
    hashed_password     TEXT,                            -- NULL if OAuth-only
    full_name           TEXT NOT NULL,
    role                user_role NOT NULL,
    oauth_provider      TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    is_verified         BOOLEAN NOT NULL DEFAULT false,
    mfa_enabled         BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_branch_role ON users(branch_id, role);

CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE permissions (
    id      SMALLSERIAL PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE     -- e.g. 'lab_order:read', 'prescription:write'
);

CREATE TABLE role_permissions (
    role            user_role NOT NULL,
    permission_id   SMALLINT NOT NULL REFERENCES permissions(id),
    PRIMARY KEY (role, permission_id)
);

-- Immutable audit log — application must never UPDATE/DELETE rows here.
CREATE TABLE audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    branch_id       UUID,
    actor_user_id   UUID,
    action          TEXT NOT NULL,          -- e.g. 'appointment.preempted'
    resource_type   TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    prev_hash       TEXT,                    -- hash chain for tamper-evidence
    row_hash        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_resource ON audit_logs(resource_type, resource_id);
CREATE INDEX idx_audit_branch_time ON audit_logs(branch_id, created_at);
-- Enforced in DB too, not just app convention:
REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC;

-- ============================================================================
-- 2. PATIENT MASTER INDEX (EMR/EHR)
-- ============================================================================

CREATE TABLE patients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mrn                 TEXT NOT NULL UNIQUE,        -- Medical Record Number, global across branches
    full_name           TEXT NOT NULL,
    dob                 DATE NOT NULL,
    sex                 TEXT NOT NULL,
    phone_encrypted     BYTEA NOT NULL,               -- PHI: field-level encrypted
    national_id_encrypted BYTEA,                      -- PHI: Aadhaar/SSN, encrypted
    national_id_hash    TEXT,                         -- deterministic hash for dedup matching (not reversible)
    phone_hash          TEXT,                         -- deterministic hash for dedup matching
    address_encrypted   BYTEA,
    allergies_encrypted BYTEA,                        -- PHI
    merged_into_id       UUID REFERENCES patients(id), -- set when this record was merged into a canonical record
    user_id              UUID UNIQUE REFERENCES users(id),
    -- Added by BACKEND-AGENT, PRPs/patient-master-index-prp.md Phase 1.
    -- Nullable: a walk-in patient registered by front desk may have no
    -- portal login yet; a portal login (Auth module, role=patient) may
    -- exist before any clinical visit creates a Patient Master Index
    -- record. UNIQUE because one login maps to at most one canonical
    -- patient. Deliberately NOT `branch_id`-scoped like other FKs in this
    -- file -- patients has no branch_id at all, by design (see the PRP's
    -- "Key design decision" section: the Patient Master Index is
    -- branch-agnostic so the same person resolves to one record across
    -- branches).
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_patients_dob_name ON patients(dob, full_name);
CREATE INDEX idx_patients_phone_hash ON patients(phone_hash);
CREATE INDEX idx_patients_national_id_hash ON patients(national_id_hash);
-- Added by BACKEND-AGENT, PRPs/patient-master-index-prp.md Phase 1: backs
-- the `similarity(full_name, :new_full_name) > 0.4` fuzzy-match query in
-- services/patient_matching.py.scan_for_duplicates(). Without this GIN
-- trigram index, that query is a full sequential scan of `patients` past a
-- few thousand rows.
CREATE INDEX idx_patients_full_name_trgm ON patients USING gin (full_name gin_trgm_ops);

-- Candidate duplicate pairs surfaced by the matching job, pending staff review.
CREATE TABLE patient_duplicate_candidates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_a_id        UUID NOT NULL REFERENCES patients(id),
    patient_b_id        UUID NOT NULL REFERENCES patients(id),
    match_score         NUMERIC(4,3) NOT NULL,        -- 0..1 probabilistic score
    match_reason        JSONB NOT NULL,                -- which fields matched deterministically/fuzzily
    status              TEXT NOT NULL DEFAULT 'pending', -- pending, confirmed_merge, rejected
    reviewed_by         UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (patient_a_id, patient_b_id)
);

-- ============================================================================
-- 3. SCHEDULING RESOURCES
-- ============================================================================

CREATE TABLE specialties (
    id      SMALLSERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE doctors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL UNIQUE REFERENCES users(id),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    specialty_id    SMALLINT NOT NULL REFERENCES specialties(id),
    default_consult_minutes SMALLINT NOT NULL DEFAULT 15,
    is_active        BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX idx_doctors_branch_specialty ON doctors(branch_id, specialty_id);

CREATE TABLE doctor_shifts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doctor_id   UUID NOT NULL REFERENCES doctors(id),
    shift_range TSTZRANGE NOT NULL,
    EXCLUDE USING gist (doctor_id WITH =, shift_range WITH &&)
);

CREATE TABLE rooms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id   UUID NOT NULL REFERENCES branches(id),
    name        TEXT NOT NULL,
    room_type   TEXT NOT NULL,       -- 'consultation', 'ot', 'procedure'
    is_active   BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE equipment (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id   UUID NOT NULL REFERENCES branches(id),
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,        -- 'imaging', 'monitor', 'surgical'
    is_mobile   BOOLEAN NOT NULL DEFAULT false,
    is_active   BOOLEAN NOT NULL DEFAULT true
);

-- ============================================================================
-- 4. TRIAGE
-- ============================================================================

CREATE TABLE triage_levels (
    level           SMALLINT PRIMARY KEY,   -- 1 = resuscitation ... 5 = non-urgent
    label           TEXT NOT NULL,
    max_wait_minutes SMALLINT NOT NULL,
    preempts         BOOLEAN NOT NULL DEFAULT false
);
INSERT INTO triage_levels VALUES
    (1, 'Resuscitation', 0, true),
    (2, 'Emergency', 10, true),
    (3, 'Urgent', 30, false),
    (4, 'Less Urgent', 60, false),
    (5, 'Non-Urgent', 120, false);

-- ============================================================================
-- 5. APPOINTMENTS — the concurrency-critical table
-- ============================================================================

CREATE TYPE appointment_status AS ENUM (
    'booked', 'checked_in', 'in_progress', 'completed',
    'cancelled', 'no_show', 'preempted'
);

CREATE TABLE appointment_series (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    doctor_id       UUID NOT NULL REFERENCES doctors(id),
    frequency       TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'biweekly')),
    occurrences     SMALLINT NOT NULL CHECK (occurrences BETWEEN 2 AND 52),
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE appointments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    doctor_id       UUID NOT NULL REFERENCES doctors(id),
    room_id         UUID NOT NULL REFERENCES rooms(id),
    time_range      TSTZRANGE NOT NULL,
    status          appointment_status NOT NULL DEFAULT 'booked',
    triage_level    SMALLINT REFERENCES triage_levels(level),  -- NULL for routine bookings
    is_emergency    BOOLEAN NOT NULL DEFAULT false,
    no_show_risk_score NUMERIC(4,3),                -- 0..1, from predictive scoring
    preempted_by_appointment_id UUID REFERENCES appointments(id),
    reschedule_of_id UUID REFERENCES appointments(id), -- points to the appointment this one replaced
    series_id       UUID REFERENCES appointment_series(id), -- NULL for a one-off booking
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Authoritative double-booking prevention: a doctor cannot have two
    -- overlapping active appointments. Cancelled/no_show/preempted rows are
    -- excluded from the constraint via the partial-exclusion WHERE clause.
    EXCLUDE USING gist (
        doctor_id WITH =,
        time_range WITH &&
    ) WHERE (status NOT IN ('cancelled', 'no_show', 'preempted'))
);
CREATE INDEX idx_appt_branch_time ON appointments USING gist (branch_id, time_range);
CREATE INDEX idx_appt_patient ON appointments(patient_id);
CREATE INDEX idx_appt_status ON appointments(status) WHERE status IN ('booked', 'checked_in');
CREATE INDEX idx_appt_series ON appointments(series_id) WHERE series_id IS NOT NULL;

-- Waiting list: distinct from the same-day walk-in queue (queue_tokens,
-- section 6 below) -- this is a patient waiting for a FUTURE slot with a
-- specific doctor to open up (e.g. every slot on their preferred date is
-- full), not a patient physically present today. Fairness rule: FIFO by
-- created_at within (doctor_id, requested_date) -- the oldest still-waiting
-- entry is offered first when a slot frees up. See services/
-- waitlist_service.py.
CREATE TYPE waitlist_status AS ENUM ('waiting', 'offered', 'fulfilled', 'cancelled');

CREATE TABLE appointment_waitlist_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    doctor_id       UUID NOT NULL REFERENCES doctors(id),
    requested_date  DATE NOT NULL,
    status          waitlist_status NOT NULL DEFAULT 'waiting',
    resolved_appointment_id UUID REFERENCES appointments(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_waitlist_doctor_date_waiting ON appointment_waitlist_entries(doctor_id, requested_date)
    WHERE status = 'waiting';

-- Room double-booking prevention (separate constraint: same table, different resource column).
CREATE TABLE appointment_room_locks (
    appointment_id  UUID PRIMARY KEY REFERENCES appointments(id) ON DELETE CASCADE,
    room_id         UUID NOT NULL REFERENCES rooms(id),
    time_range      TSTZRANGE NOT NULL,
    EXCLUDE USING gist (room_id WITH =, time_range WITH &&)
);

-- Equipment double-booking prevention (many-to-many: one appointment can need N equipment).
CREATE TABLE appointment_equipment_locks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    appointment_id  UUID NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    equipment_id    UUID NOT NULL REFERENCES equipment(id),
    time_range      TSTZRANGE NOT NULL,
    EXCLUDE USING gist (equipment_id WITH =, time_range WITH &&)
);

-- ============================================================================
-- 6. REAL-TIME QUEUE / TOKENS
-- ============================================================================

CREATE TYPE token_status AS ENUM ('waiting', 'in_consultation', 'delayed', 'skipped', 'done');

CREATE TABLE queue_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    appointment_id  UUID REFERENCES appointments(id),
    department_id   SMALLINT REFERENCES specialties(id),
    token_number    INTEGER NOT NULL,
    status          token_status NOT NULL DEFAULT 'waiting',
    checked_in_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    called_at       TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    estimated_wait_minutes SMALLINT,
    -- HMS Project Completion Prompt gap ("emergency queue priority"): set
    -- explicitly at check-in (walk-in) or inherited from the linked
    -- appointment's `is_emergency` flag (see services/queue_service.py's
    -- `check_in`) -- `list_queue` orders `is_priority DESC, checked_in_at
    -- ASC`, so priority tokens jump the FIFO line but are still ordered
    -- fairly among themselves by arrival time.
    is_priority     BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX idx_queue_branch_dept_status ON queue_tokens(branch_id, department_id, status);

-- ============================================================================
-- 7. CLINICAL CONSULTATION & PRESCRIPTIONS
-- ============================================================================

CREATE TABLE consultations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    appointment_id  UUID NOT NULL UNIQUE REFERENCES appointments(id),
    doctor_id       UUID NOT NULL REFERENCES doctors(id),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    symptoms        TEXT,
    notes_encrypted BYTEA,                   -- PHI
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

CREATE TABLE diagnoses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id UUID NOT NULL REFERENCES consultations(id),
    icd_code        TEXT NOT NULL,             -- ICD-10/11
    description     TEXT NOT NULL,
    is_primary      BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE patient_allergies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    substance       TEXT NOT NULL,
    severity        TEXT NOT NULL,             -- mild, moderate, severe
    reaction        TEXT
);
CREATE INDEX idx_allergies_patient ON patient_allergies(patient_id);

CREATE TABLE drugs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    generic_name    TEXT,
    interaction_class TEXT                     -- used by the interaction checker
);

CREATE TABLE drug_interactions (
    drug_a_id       UUID NOT NULL REFERENCES drugs(id),
    drug_b_id       UUID NOT NULL REFERENCES drugs(id),
    severity        TEXT NOT NULL,             -- contraindicated, major, moderate, minor
    description     TEXT,
    PRIMARY KEY (drug_a_id, drug_b_id)
);

CREATE TABLE prescriptions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id UUID NOT NULL REFERENCES consultations(id),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft, finalized, dispensed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prescription_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prescription_id UUID NOT NULL REFERENCES prescriptions(id),
    drug_id         UUID NOT NULL REFERENCES drugs(id),
    dosage          TEXT NOT NULL,
    frequency       TEXT NOT NULL,
    -- CHECK added by REVIEW-AGENT, PRPs/clinical-consultation-prescription-prp.md
    -- Phase 3 (finding H1): defense-in-depth alongside the Pydantic
    -- Field(ge=1) on PrescriptionItemCreate -- a zero/negative value here
    -- would make the safety engine's "is this prescription still active"
    -- window compute as already-expired the moment it's written, silently
    -- hiding a real drug from the interaction checker.
    duration_days   SMALLINT CHECK (duration_days IS NULL OR duration_days > 0)
);

-- ============================================================================
-- 8. LAB
-- ============================================================================

CREATE TYPE lab_sample_status AS ENUM ('ordered', 'collected', 'processing', 'verified', 'attached');

CREATE TABLE lab_orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id UUID NOT NULL REFERENCES consultations(id),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    test_code       TEXT NOT NULL,
    ordered_by      UUID NOT NULL REFERENCES users(id),
    status          lab_sample_status NOT NULL DEFAULT 'ordered',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE lab_samples (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- UNIQUE added by REVIEW-AGENT, PRPs/lab-module-prp.md Phase 3 (finding
    -- H1): without it, two near-simultaneous `ordered -> collected`
    -- transitions for the same order (an ordinary race, not an exotic one --
    -- two staff acting on the same physical sample) could both pass the
    -- app-level "does a sample already exist?" check and both INSERT,
    -- leaving two sample rows for one order -- every later read/transition
    -- then raises MultipleResultsFound (500), permanently breaking the
    -- order. This is authoritative, DB-enforced backstop; services/
    -- lab_service.py additionally takes a row lock on the LabOrder for the
    -- same reason `scheduling_engine.py` layers a Redis lock UNDER a
    -- Postgres constraint rather than relying on either alone.
    lab_order_id    UUID NOT NULL UNIQUE REFERENCES lab_orders(id),
    collected_by    UUID REFERENCES users(id),
    collected_at    TIMESTAMPTZ,
    processed_at    TIMESTAMPTZ,
    verified_by     UUID REFERENCES users(id),
    verified_at     TIMESTAMPTZ,
    result_encrypted BYTEA,                     -- PHI
    attached_to_emr_at TIMESTAMPTZ
);

-- ============================================================================
-- 9. PHARMACY INVENTORY (FEFO)
-- ============================================================================

CREATE TABLE inventory_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    drug_id         UUID NOT NULL REFERENCES drugs(id),
    reorder_threshold INTEGER NOT NULL DEFAULT 0,
    UNIQUE (branch_id, drug_id)
);

CREATE TABLE inventory_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_item_id UUID NOT NULL REFERENCES inventory_items(id),
    batch_number    TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity >= 0),
    expiry_date     DATE NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- FEFO retrieval: ORDER BY expiry_date ASC
CREATE INDEX idx_batches_fefo ON inventory_batches(inventory_item_id, expiry_date);

-- ============================================================================
-- 10. WARDS / BEDS / OT
-- ============================================================================

CREATE TABLE wards (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id   UUID NOT NULL REFERENCES branches(id),
    name        TEXT NOT NULL,
    ward_type   TEXT NOT NULL          -- 'icu', 'general', 'private'
);

CREATE TYPE bed_status AS ENUM ('available', 'occupied', 'cleaning', 'blocked');

CREATE TABLE beds (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ward_id     UUID NOT NULL REFERENCES wards(id),
    label       TEXT NOT NULL,
    status      bed_status NOT NULL DEFAULT 'available',
    UNIQUE (ward_id, label)
);

CREATE TABLE admissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    bed_id          UUID NOT NULL REFERENCES beds(id),
    stay_range      TSTZRANGE NOT NULL,
    admitted_by     UUID NOT NULL REFERENCES users(id),
    discharged_at   TIMESTAMPTZ,
    EXCLUDE USING gist (bed_id WITH =, stay_range WITH &&)
);

CREATE TABLE ot_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id         UUID NOT NULL REFERENCES rooms(id),   -- room_type = 'ot'
    patient_id      UUID NOT NULL REFERENCES patients(id),
    surgeon_id      UUID NOT NULL REFERENCES doctors(id),
    time_range      TSTZRANGE NOT NULL,
    EXCLUDE USING gist (room_id WITH =, time_range WITH &&)
);

-- ============================================================================
-- 11. BILLING / INSURANCE
-- ============================================================================

CREATE TABLE invoices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    status          TEXT NOT NULL DEFAULT 'open',  -- open, split, paid, void
    total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    -- HMS Project Completion Prompt gap ("tax and discount handling"):
    -- invoice-level, not per-item -- `tax_rate_percent` applies to
    -- (total_amount - discount_amount), matching common real-world
    -- ordering (discount first, then tax on the discounted subtotal).
    -- Both default to zero/null so every pre-existing invoice (and every
    -- invoice that never calls the new adjustments endpoint) computes a
    -- grand_total identical to total_amount, unchanged.
    tax_rate_percent    NUMERIC(5,2),
    discount_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Added by BACKEND-AGENT, PRPs/billing-module-prp.md Phase 1: plain
    -- TEXT (matching `Prescription.status`'s convention, not a `CREATE
    -- TYPE` enum -- see that model's docstring for the reasoning this
    -- mirrors) still gets a DB-level CHECK as a cheap backstop, since this
    -- table holds financial state.
    CONSTRAINT invoices_status_valid CHECK (status IN ('open', 'split', 'paid', 'void')),
    CONSTRAINT invoices_total_amount_non_negative CHECK (total_amount >= 0),
    CONSTRAINT invoices_tax_rate_percent_range CHECK (tax_rate_percent IS NULL OR (tax_rate_percent >= 0 AND tax_rate_percent <= 100)),
    CONSTRAINT invoices_discount_amount_non_negative CHECK (discount_amount >= 0),
    CONSTRAINT invoices_discount_not_exceeding_total CHECK (discount_amount <= total_amount)
);

CREATE TABLE invoice_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id),
    source_type     TEXT NOT NULL,     -- 'consultation', 'lab_order', 'prescription', 'admission'
    source_id       UUID NOT NULL,
    description     TEXT NOT NULL,
    amount          NUMERIC(12,2) NOT NULL,
    CONSTRAINT invoice_items_amount_positive CHECK (amount > 0),
    CONSTRAINT invoice_items_source_type_valid
        CHECK (source_type IN ('consultation', 'lab_order', 'prescription', 'admission')),
    -- Authoritative, DB-level guard against double-billing the same
    -- clinical event across ANY invoice -- the money equivalent of the
    -- `EXCLUDE USING gist` two-layer discipline this codebase uses
    -- everywhere else (app-level check + DB backstop). A plain UNIQUE
    -- suffices here (no time-range overlap involved, just "this exact
    -- event has already been invoiced, full stop").
    CONSTRAINT invoice_items_source_unique UNIQUE (source_type, source_id)
);

CREATE TABLE insurance_claims (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id),
    payer_name      TEXT NOT NULL,
    claim_amount    NUMERIC(12,2) NOT NULL,
    patient_copay   NUMERIC(12,2) NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'submitted', -- submitted, adjudicating, approved, denied, paid
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT insurance_claims_state_valid
        CHECK (state IN ('submitted', 'adjudicating', 'approved', 'denied', 'paid')),
    CONSTRAINT insurance_claims_claim_amount_non_negative CHECK (claim_amount >= 0),
    CONSTRAINT insurance_claims_patient_copay_non_negative CHECK (patient_copay >= 0)
);

-- ============================================================================
-- 12. NOTIFICATIONS
-- ============================================================================

CREATE TABLE notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id),
    patient_id      UUID REFERENCES patients(id),
    -- Added by BACKEND-AGENT, PRPs/notification-hub-prp.md Phase 1: nullable
    -- because this table (unlike Invoice) has no other column that always
    -- carries a branch -- the consumer resolves it per-event (directly off
    -- the payload for `appointment.booked`, via an `Appointment` lookup for
    -- the other three current topics) and a future topic that genuinely
    -- can't resolve one leaves this NULL rather than inventing a value.
    branch_id       UUID REFERENCES branches(id),
    channel         TEXT NOT NULL,          -- sms, email, push
    template        TEXT NOT NULL,          -- appointment_reminder, token_called, lab_ready, receipt
    payload         JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued, sent, failed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT notifications_channel_valid CHECK (channel IN ('sms', 'email', 'push')),
    CONSTRAINT notifications_status_valid CHECK (status IN ('queued', 'sent', 'failed'))
);

-- ============================================================================
-- 13. ROW-LEVEL SECURITY TEMPLATE (defense in depth for tenancy)
-- ============================================================================

ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON appointments
    USING (branch_id = current_setting('app.current_branch_id', true)::uuid);
-- Repeat per branch-scoped table; app sets `app.current_branch_id` via
-- `SET LOCAL` at the start of each request-scoped transaction.

-- Added by REVIEW-AGENT, PRPs/pharmacy-module-prp.md Phase 3 (finding M1):
-- Pharmacy is the first module in the build sequence where branch tenancy
-- is load-bearing (Patient/Consultation/Lab are all deliberately
-- branch-agnostic -- see their own PRPs), so it's the first place this
-- template's own "repeat per branch-scoped table" instruction actually
-- matters and had been left unaddressed. Matches the `appointments` policy
-- above exactly -- see that policy's caveat about table ownership/BYPASSRLS
-- applying equally here; this is unchanged, template-consistent
-- defense-in-depth, not a new enforcement guarantee beyond what
-- `appointments` already has.
ALTER TABLE inventory_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON inventory_items
    USING (branch_id = current_setting('app.current_branch_id', true)::uuid);

-- inventory_batches has no branch_id column of its own -- scope through its
-- parent inventory_item, the same relationship every application-layer
-- query in services/pharmacy_engine.py / services/pharmacy_service.py
-- already joins on.
ALTER TABLE inventory_batches ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON inventory_batches
    USING (inventory_item_id IN (
        SELECT id FROM inventory_items
        WHERE branch_id = current_setting('app.current_branch_id', true)::uuid
    ));

-- Added by REVIEW-AGENT, PRPs/ward-bed-ot-module-prp.md Phase 3 (finding M1):
-- the exact same gap class Pharmacy's own Phase 3 review found (M1) --
-- `wards` has a real branch_id column and was missing its RLS backstop
-- despite the template's "repeat per branch-scoped table" instruction and
-- Pharmacy having just added the equivalent for inventory_items.
ALTER TABLE wards ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON wards
    USING (branch_id = current_setting('app.current_branch_id', true)::uuid);

-- beds/admissions have no branch_id column of their own -- scope through
-- their parent ward, the same relationship services/ward_service.py's
-- queries already join on (Bed -> Ward, Admission -> Bed -> Ward).
ALTER TABLE beds ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON beds
    USING (ward_id IN (
        SELECT id FROM wards
        WHERE branch_id = current_setting('app.current_branch_id', true)::uuid
    ));

ALTER TABLE admissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON admissions
    USING (bed_id IN (
        SELECT b.id FROM beds b
        JOIN wards w ON w.id = b.ward_id
        WHERE w.branch_id = current_setting('app.current_branch_id', true)::uuid
    ));

-- ot_schedules scopes through room_id -> rooms.branch_id (rooms already
-- carry branch_id directly, same relationship services/ward_engine.py's
-- schedule_ot / services/ward_service.py's list_ot_schedules already join on).
ALTER TABLE ot_schedules ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON ot_schedules
    USING (room_id IN (
        SELECT id FROM rooms
        WHERE branch_id = current_setting('app.current_branch_id', true)::uuid
    ));

-- Added by BACKEND-AGENT, PRPs/billing-module-prp.md Phase 1 -- `invoices`
-- has a real branch_id column (added at creation time; see PRP for why
-- billing is scoped to the branch where the chargeable event happened,
-- not floated across a patient's whole multi-branch record).
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON invoices
    USING (branch_id = current_setting('app.current_branch_id', true)::uuid);

-- invoice_items/insurance_claims have no branch_id of their own -- scope
-- through their parent invoice, same "no branch_id column, join to the
-- resource that has one" relationship beds/admissions use above.
ALTER TABLE invoice_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON invoice_items
    USING (invoice_id IN (
        SELECT id FROM invoices
        WHERE branch_id = current_setting('app.current_branch_id', true)::uuid
    ));

ALTER TABLE insurance_claims ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON insurance_claims
    USING (invoice_id IN (
        SELECT id FROM invoices
        WHERE branch_id = current_setting('app.current_branch_id', true)::uuid
    ));

-- Added by BACKEND-AGENT, PRPs/notification-hub-prp.md Phase 1 -- `branch_id`
-- is nullable here (a future topic that can't resolve one leaves it NULL,
-- see models/notification.py), so a row with no resolved branch is only
-- visible to a caller with no branch filter set at all (system_admin) --
-- `= current_setting(...)` never matches a NULL column, so a NULL-branch_id
-- row is correctly invisible to every non-admin caller by default, not a
-- special case needing extra SQL.
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_isolation ON notifications
    USING (branch_id = current_setting('app.current_branch_id', true)::uuid);
