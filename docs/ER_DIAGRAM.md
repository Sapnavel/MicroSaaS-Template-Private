# Entity-Relationship Diagram

Generated directly from `database/schema.sql` (the authoritative DDL — this
diagram is kept in sync with it, not a separate design artifact). 39 tables
across 13 sections. Attribute lists below are trimmed to primary keys,
foreign keys, and fields that materially affect relationships or business
rules — see `database/schema.sql` itself for the full column list, all
`CHECK`/`UNIQUE` constraints, and every index.

```mermaid
erDiagram
    HOSPITAL_GROUPS ||--o{ BRANCHES : "has"
    BRANCHES ||--o{ USERS : "employs"
    BRANCHES ||--o{ DOCTORS : "has"
    BRANCHES ||--o{ ROOMS : "has"
    BRANCHES ||--o{ EQUIPMENT : "has"
    BRANCHES ||--o{ APPOINTMENTS : "hosts"
    BRANCHES ||--o{ WARDS : "has"
    BRANCHES ||--o{ INVENTORY_ITEMS : "stocks"
    BRANCHES ||--o{ INVOICES : "bills"
    BRANCHES ||--o{ QUEUE_TOKENS : "queues"

    USERS ||--o{ REFRESH_TOKENS : "issues"
    USERS ||--o| DOCTORS : "is (if role=doctor)"
    USERS ||--o| PATIENTS : "is (if role=patient)"
    ROLE_PERMISSIONS }o--|| PERMISSIONS : "grants"

    PATIENTS ||--o{ PATIENT_DUPLICATE_CANDIDATES : "flagged in"
    PATIENTS ||--o{ APPOINTMENTS : "books"
    PATIENTS ||--o{ APPOINTMENT_WAITLIST_ENTRIES : "waits on"
    PATIENTS ||--o{ CONSULTATIONS : "attends"
    PATIENTS ||--o{ PATIENT_ALLERGIES : "has"
    PATIENTS ||--o{ PRESCRIPTIONS : "receives"
    PATIENTS ||--o{ LAB_ORDERS : "has"
    PATIENTS ||--o{ ADMISSIONS : "is admitted (as)"
    PATIENTS ||--o{ OT_SCHEDULES : "undergoes"
    PATIENTS ||--o{ INVOICES : "is billed"
    PATIENTS ||--o| PATIENTS : "merged_into"

    SPECIALTIES ||--o{ DOCTORS : "practiced by"
    SPECIALTIES ||--o{ QUEUE_TOKENS : "departments"
    DOCTORS ||--o{ DOCTOR_SHIFTS : "works"
    DOCTORS ||--o{ APPOINTMENTS : "sees"
    DOCTORS ||--o{ APPOINTMENT_SERIES : "recurs for"
    DOCTORS ||--o{ APPOINTMENT_WAITLIST_ENTRIES : "waited for"
    DOCTORS ||--o{ CONSULTATIONS : "conducts"
    DOCTORS ||--o{ OT_SCHEDULES : "operates (as surgeon)"

    TRIAGE_LEVELS ||--o{ APPOINTMENTS : "classifies"

    APPOINTMENT_SERIES ||--o{ APPOINTMENTS : "generates"
    APPOINTMENTS ||--o| APPOINTMENT_ROOM_LOCKS : "locks a room"
    APPOINTMENTS ||--o{ APPOINTMENT_EQUIPMENT_LOCKS : "locks equipment"
    APPOINTMENTS ||--o| APPOINTMENTS : "preempted_by / reschedule_of"
    APPOINTMENTS ||--o| CONSULTATIONS : "produces"
    APPOINTMENTS ||--o{ QUEUE_TOKENS : "checked into"
    ROOMS ||--o{ APPOINTMENT_ROOM_LOCKS : "reserved via"
    ROOMS ||--o{ APPOINTMENTS : "hosts"
    ROOMS ||--o{ OT_SCHEDULES : "hosts (OT room)"
    EQUIPMENT ||--o{ APPOINTMENT_EQUIPMENT_LOCKS : "reserved via"

    CONSULTATIONS ||--o{ DIAGNOSES : "records"
    CONSULTATIONS ||--o{ PRESCRIPTIONS : "authorizes"
    CONSULTATIONS ||--o{ LAB_ORDERS : "orders"

    DRUGS ||--o{ DRUG_INTERACTIONS : "drug_a"
    DRUGS ||--o{ DRUG_INTERACTIONS : "drug_b"
    DRUGS ||--o{ PRESCRIPTION_ITEMS : "prescribed as"
    DRUGS ||--o{ INVENTORY_ITEMS : "stocked as"
    PRESCRIPTIONS ||--o{ PRESCRIPTION_ITEMS : "lists"

    LAB_ORDERS ||--o| LAB_SAMPLES : "tracked by"

    INVENTORY_ITEMS ||--o{ INVENTORY_BATCHES : "received as"

    WARDS ||--o{ BEDS : "contains"
    BEDS ||--o{ ADMISSIONS : "hosts"

    INVOICES ||--o{ INVOICE_ITEMS : "itemizes"
    INVOICES ||--o{ INSURANCE_CLAIMS : "splits into"

    USERS ||--o{ NOTIFICATIONS : "receives"
    PATIENTS ||--o{ NOTIFICATIONS : "receives"
    BRANCHES ||--o{ NOTIFICATIONS : "scopes"

    HOSPITAL_GROUPS {
        uuid id PK
        text name
    }
    BRANCHES {
        uuid id PK
        uuid hospital_group_id FK
        text name
        text timezone
    }
    USERS {
        uuid id PK
        uuid branch_id FK "NULL for system_admin"
        text email UK
        user_role role "enum"
        bool mfa_enabled
    }
    REFRESH_TOKENS {
        uuid id PK
        uuid user_id FK
        text token_hash UK
        timestamptz expires_at
    }
    PERMISSIONS {
        smallint id PK
        text code UK
    }
    ROLE_PERMISSIONS {
        user_role role PK
        smallint permission_id PK_FK
    }
    AUDIT_LOGS {
        bigint id PK
        uuid branch_id
        uuid actor_user_id
        text action
        text resource_type
        text row_hash "hash chain"
    }
    PATIENTS {
        uuid id PK
        text mrn UK
        text full_name
        date dob
        bytea phone_encrypted "PHI"
        text phone_hash "dedup"
        uuid merged_into_id FK
        uuid user_id FK_UK
    }
    PATIENT_DUPLICATE_CANDIDATES {
        uuid id PK
        uuid patient_a_id FK
        uuid patient_b_id FK
        numeric match_score
        text status
    }
    SPECIALTIES {
        smallint id PK
        text name UK
    }
    DOCTORS {
        uuid id PK
        uuid user_id FK_UK
        uuid branch_id FK
        smallint specialty_id FK
    }
    DOCTOR_SHIFTS {
        uuid id PK
        uuid doctor_id FK
        tstzrange shift_range "EXCLUDE gist"
    }
    ROOMS {
        uuid id PK
        uuid branch_id FK
        text room_type
    }
    EQUIPMENT {
        uuid id PK
        uuid branch_id FK
        text category
    }
    TRIAGE_LEVELS {
        smallint level PK
        text label
        bool preempts
    }
    APPOINTMENT_SERIES {
        uuid id PK
        uuid branch_id FK
        uuid patient_id FK
        uuid doctor_id FK
        text frequency
        smallint occurrences
    }
    APPOINTMENTS {
        uuid id PK
        uuid branch_id FK
        uuid patient_id FK
        uuid doctor_id FK
        uuid room_id FK
        tstzrange time_range "EXCLUDE gist w/ doctor_id"
        appointment_status status "enum"
        smallint triage_level FK
        bool is_emergency
        numeric no_show_risk_score
        uuid preempted_by_appointment_id FK
        uuid reschedule_of_id FK
        uuid series_id FK
    }
    APPOINTMENT_WAITLIST_ENTRIES {
        uuid id PK
        uuid branch_id FK
        uuid patient_id FK
        uuid doctor_id FK
        date requested_date
        waitlist_status status "enum"
        uuid resolved_appointment_id FK
    }
    APPOINTMENT_ROOM_LOCKS {
        uuid appointment_id PK_FK
        uuid room_id FK
        tstzrange time_range "EXCLUDE gist"
    }
    APPOINTMENT_EQUIPMENT_LOCKS {
        uuid id PK
        uuid appointment_id FK
        uuid equipment_id FK
        tstzrange time_range "EXCLUDE gist"
    }
    QUEUE_TOKENS {
        uuid id PK
        uuid branch_id FK
        uuid appointment_id FK
        smallint department_id FK
        integer token_number
        token_status status "enum"
        bool is_priority
    }
    CONSULTATIONS {
        uuid id PK
        uuid appointment_id FK_UK
        uuid doctor_id FK
        uuid patient_id FK
        bytea notes_encrypted "PHI"
        timestamptz started_at
        timestamptz ended_at
    }
    DIAGNOSES {
        uuid id PK
        uuid consultation_id FK
        text icd_code
        bool is_primary
    }
    PATIENT_ALLERGIES {
        uuid id PK
        uuid patient_id FK
        text substance
        text severity
    }
    DRUGS {
        uuid id PK
        text name
        text generic_name
        text interaction_class
    }
    DRUG_INTERACTIONS {
        uuid drug_a_id PK_FK
        uuid drug_b_id PK_FK
        text severity
    }
    PRESCRIPTIONS {
        uuid id PK
        uuid consultation_id FK
        uuid patient_id FK
        text status
    }
    PRESCRIPTION_ITEMS {
        uuid id PK
        uuid prescription_id FK
        uuid drug_id FK
        text dosage
        smallint duration_days "NULL=ongoing"
    }
    LAB_ORDERS {
        uuid id PK
        uuid consultation_id FK
        uuid patient_id FK
        text test_code
        uuid ordered_by FK
        lab_sample_status status "enum"
    }
    LAB_SAMPLES {
        uuid id PK
        uuid lab_order_id FK_UK
        uuid collected_by FK
        uuid verified_by FK
        bytea result_encrypted "PHI"
    }
    INVENTORY_ITEMS {
        uuid id PK
        uuid branch_id FK
        uuid drug_id FK
        integer reorder_threshold
    }
    INVENTORY_BATCHES {
        uuid id PK
        uuid inventory_item_id FK
        text batch_number
        integer quantity "CHECK >= 0"
        date expiry_date
    }
    WARDS {
        uuid id PK
        uuid branch_id FK
        text ward_type
    }
    BEDS {
        uuid id PK
        uuid ward_id FK
        text label
        bed_status status "enum"
    }
    ADMISSIONS {
        uuid id PK
        uuid patient_id FK
        uuid bed_id FK
        tstzrange stay_range "EXCLUDE gist"
        uuid admitted_by FK
        timestamptz discharged_at
    }
    OT_SCHEDULES {
        uuid id PK
        uuid room_id FK
        uuid patient_id FK
        uuid surgeon_id FK
        tstzrange time_range "EXCLUDE gist"
    }
    INVOICES {
        uuid id PK
        uuid patient_id FK
        uuid branch_id FK
        text status
        numeric total_amount "CHECK >= 0"
        numeric tax_rate_percent "CHECK 0-100"
        numeric discount_amount "CHECK <= total"
    }
    INVOICE_ITEMS {
        uuid id PK
        uuid invoice_id FK
        text source_type
        uuid source_id "polymorphic, no FK"
        numeric amount "CHECK > 0"
    }
    INSURANCE_CLAIMS {
        uuid id PK
        uuid invoice_id FK
        text payer_name
        numeric claim_amount
        numeric patient_copay
        text state
    }
    NOTIFICATIONS {
        uuid id PK
        uuid user_id FK
        uuid patient_id FK
        uuid branch_id FK
        text channel "sms/email/push"
        text status
    }
```

## Key relationship notes

- **`patients` has no `branch_id`** — deliberately branch-agnostic (the
  Patient Master Index resolves one canonical record per person across every
  branch). Every clinical/billing table that references a patient carries
  its own `branch_id` instead.
- **Concurrency-critical exclusion constraints** (`EXCLUDE USING gist`, all
  requiring the `btree_gist` extension): `doctor_shifts`, `appointments`
  (doctor × time, partial — excludes cancelled/no_show/preempted),
  `appointment_room_locks`, `appointment_equipment_locks`, `admissions`
  (bed × stay), `ot_schedules` (room × time). These are the actual,
  DB-enforced double-booking guards — see `docs/ARCHITECTURE.md` section 5.
- **`invoice_items.source_id` is polymorphic** (no FK) — it points at
  `consultations`, `lab_orders`, `prescriptions`, or `admissions` depending
  on `source_type`; `invoice_items_source_unique UNIQUE(source_type,
  source_id)` is what actually prevents double-billing the same clinical
  event.
- **PHI fields are encrypted at the column level** (`BYTEA`, `*_encrypted`
  suffix): `patients.phone_encrypted`/`national_id_encrypted`/
  `address_encrypted`/`allergies_encrypted`, `consultations.notes_encrypted`,
  `lab_samples.result_encrypted`. Deterministic hash columns
  (`phone_hash`, `national_id_hash`) exist alongside the encrypted values
  purely for dedup matching — they are one-way and never decrypted back to
  the original value.
- **`audit_logs` is hash-chained and DB-locked against mutation**
  (`REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC`) — tamper-evidence at
  the database level, not just an application convention.
- **Row-Level Security** is enabled on every branch-scoped table (directly
  or transitively through a parent FK) as defense-in-depth behind the
  application-layer tenant guard — see schema.sql section 13.

For full column lists, every `CHECK`/`UNIQUE` constraint, and every index,
read `database/schema.sql` directly — it is intentionally the single source
of truth this diagram is generated from, not a parallel spec that can drift.
