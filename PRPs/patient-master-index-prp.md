# PRP: Patient Master Index (EMR/EHR) & Deduplication Module

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> and the just-completed Auth/RBAC module (`PRPs/auth-rbac-abac-prp.md`).

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Patient Master Index (EMR/EHR) & Deduplication |
| **Version** | 1.0 |
| **Created** | 2026-07-28 |
| **Complexity** | Medium-High (matching algorithm + merge workflow touches referential integrity across modules) |

---

## MODULE OVERVIEW

**Description:** The single source of truth for patient identity across all
branches. Patients can be created by front-desk staff (walk-in registration)
independently of the patient portal login created in the Auth module — this
module is what makes "the same human" resolvable to one canonical record
even when they were registered twice (different branches, a typo'd name, a
walk-in visit before ever creating a portal account).

**Why this module, why now:** Every clinical module still to be built
(consultations, lab, pharmacy, wards, billing) hangs off `patients.id`. Get
identity resolution right here or every downstream module inherits duplicate
records silently. This is also the first module to actually use
`patient_duplicate_candidates`, which existed in the schema since the initial
scaffold but had no engine behind it.

**What already exists (do not recreate):**
- `database/schema.sql` §2 — `patients` table (mrn, full_name, dob, sex,
  `phone_encrypted`/`national_id_encrypted`/`address_encrypted`/`allergies_encrypted`
  PHI fields via `EncryptedString`, `phone_hash`/`national_id_hash`
  deterministic-hash columns for exact-match lookup without decrypting,
  `merged_into_id` self-FK for merge tombstoning), `patient_duplicate_candidates`
  table (patient_a_id, patient_b_id, match_score, match_reason JSONB, status,
  reviewed_by).
- `backend/app/models/patient.py` — `Patient` ORM model (already wired to
  `EncryptedString`/`deterministic_hash` from the Auth module's PHI work).
- `backend/app/core/encryption.py` — `EncryptedString` type, `deterministic_hash()`.
- `backend/app/core/security.py` — `authorize()`, and an existing example
  policy `_front_desk_reads_demographics_only` (`@policy("front_desk", "patient", "read")`)
  that currently just returns `True` — this PRP is what gives it a router to
  actually gate, and what the "demographics only" half of its name needs to
  become real (response-shaping, not just row access).
- `backend/app/routers/patients.py` — stub with a TODO list; this PRP
  replaces it.
- `backend/app/routers/admin.py`, `services/auth_service.py` — the
  `POST /admin/staff` pattern to copy for router/service separation.

**Key design decision this PRP makes (read before implementing):**
`patients` has **no `branch_id`** column, by design — the Patient Master
Index is branch-agnostic on purpose (a patient seen at Branch A and later at
Branch B must resolve to the same record; see `docs/ARCHITECTURE.md` module
2 description). This means `core/security.py`'s tenant guard in `authorize()`
(which only fires `if hasattr(resource, "branch_id")`) **does not apply** to
patients — that's correct, not a gap: any authenticated staff role with a
registered `patient` policy can reach any patient's demographic row
regardless of branch, and role-based field restriction (front_desk vs.
clinical roles) is the actual control here, not tenant isolation. Say this
explicitly in code comments where it might look like an oversight.

**MVP Scope:**
- [ ] `patients.user_id` link from a portal login (Auth module) to its
  canonical clinical record (nullable — walk-ins may have no login yet)
- [ ] Deterministic dedup check (exact phone/national-ID hash, or exact
  DOB+normalized-name) at patient-creation time
- [ ] Probabilistic dedup scan (fuzzy name + DOB proximity via Postgres
  trigram similarity) populating `patient_duplicate_candidates` for staff review
- [ ] Search-before-create endpoint so front desk can find an existing
  patient before accidentally creating a duplicate (the highest-leverage
  fix — cheaper than merging after the fact)
- [ ] Merge workflow: reviewer picks a survivor, loser's `merged_into_id` is
  set, FKs on already-implemented modules (currently just `appointments`)
  are reassigned to the survivor in the same transaction
- [ ] RBAC/ABAC: front_desk gets demographics only; clinical roles
  (doctor/nurse/system_admin) get full record including allergies

---

## TECH STACK

| Layer | Technology | Skill Reference |
|-------|------------|-----------------|
| Backend | FastAPI + Python 3.11+ | skills/BACKEND.md |
| Frontend | React + TypeScript + Vite | skills/FRONTEND.md |
| Database | PostgreSQL + SQLAlchemy + `pg_trgm` (new extension, see Schema Changes) | skills/DATABASE.md |
| Matching | Postgres trigram similarity (`pg_trgm`) for fuzzy name matching — no new Python dependency, keeps matching in the DB where the candidate set is indexed instead of pulling the whole `patients` table into app memory | — |

---

## SCHEMA CHANGES (additive migration — do not hand-edit existing rows' shape)

Add to `database/schema.sql` §2 (PATIENT MASTER INDEX), and mirror in
`backend/app/models/patient.py`:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE patients ADD COLUMN user_id UUID UNIQUE REFERENCES users(id);
-- Nullable: a walk-in patient registered by front desk may have no portal
-- login yet; a portal login (Auth module, role=patient) may exist before
-- any clinical visit creates a Patient Master Index record. UNIQUE because
-- one login maps to at most one canonical patient.

CREATE INDEX idx_patients_full_name_trgm ON patients USING gin (full_name gin_trgm_ops);
-- Backs the fuzzy-match query in services/patient_matching.py — without
-- this index, a `similarity(full_name, :name) > threshold` scan is a full
-- table scan past a few thousand patients.
```

No changes to existing columns. Write this as a proper additive migration
(either hand-add to `database/schema.sql` directly, matching how the file
already documents its own history in comments — see the `updated_at`
addition the Auth module's TEST-AGENT made — or an Alembic revision if you
set one up; either is fine, but do not silently diverge schema.sql from
what the ORM models expect, that exact bug already bit the Auth module once).

---

## MATCHING ENGINE DESIGN (read before implementing — this is the core logic)

`backend/app/services/patient_matching.py`, mirroring the rigor of
`services/scheduling_engine.py` (this module's engine is the "hard part,"
same as tri-resource booking was for scheduling):

### Deterministic pass (synchronous, blocks patient creation)
Exact match on any of:
- `phone_hash` equality
- `national_id_hash` equality (when provided — not all patients have one)
- `dob` equality AND normalized `full_name` equality (lowercased, trimmed,
  whitespace-collapsed)

If a deterministic match is found at creation time: **do not silently
create a duplicate**. Return a `409 Conflict` with the existing patient's
id/MRN so the front-desk UI can offer "use existing record" instead of
proceeding — this is cheaper and safer than creating-then-merging.
Provide an explicit `force=true` query param / body flag for the rare
legitimate case of two different people sharing a phone number's hash
collision-adjacent... no, actually: same phone_hash almost never means
"different person," but allow an explicit override for staff judgment
calls, audit-logged when used.

### Probabilistic pass (runs synchronously right after a successful create,
since there's no background job runner wired up yet — see note)
For the newly created patient, query existing patients (excluding the one
just created, excluding already-`merged_into_id`-tombstoned rows) where:
- `dob` is within 0 days (exact — DOB is high-signal, don't fuzz it) AND
- `similarity(full_name, :new_full_name) > 0.4` (Postgres `pg_trgm`)

For each candidate hit, compute a weighted match score:

| Signal | Condition | Weight |
|---|---|---|
| phone_hash | exact match | 0.40 |
| national_id_hash | exact match (only if both non-null) | 0.45 |
| dob | exact match | 0.15 |
| full_name | trigram similarity, scaled | up to 0.25 (× similarity score) |

Sum weights (cap at 1.0). If `score >= 0.25`, insert a row into
`patient_duplicate_candidates` (`match_reason` JSONB records which signals
fired and their individual contribution — reviewers need to see *why* it
matched, not just the number). Do not auto-merge at any score, however
high — per the PRP's clinical-safety framing, a wrong merge corrupts two
people's medical histories together; merge is always a human decision made
through the review endpoint below.

**Threshold correction (post Phase 1 validation):** the original draft of
this PRP said `score >= 0.5`. That's mathematically wrong given the weights
above: `DOB_WEIGHT` (0.15) + `FULL_NAME_TRIGRAM_WEIGHT` (0.25) tops out at
0.40 even at perfect name similarity, so a DOB+name-typo match — the
scenario this scan exists to catch — could never reach 0.5 unless
phone/national_id also matched, which the deterministic pass already
intercepts earlier. Use **0.25** (the floor of what the SQL prefilter's own
`similarity > 0.4` condition can produce), so the score threshold ranks
candidates for review rather than silently discarding everything the SQL
prefilter just qualified. See `services/patient_matching.py`'s
`DUPLICATE_CANDIDATE_SCORE_THRESHOLD` for the authoritative value.

**Note on "should this be async":** `docs/ARCHITECTURE.md`'s event bus
(RabbitMQ) exists for exactly this kind of decoupled background work, but
wiring a consumer process is out of scope for this PRP (no consumer
infrastructure exists yet for any module). Running the probabilistic pass
synchronously in the create-patient request is the pragmatic choice now;
leave a clearly marked seam (a single function call,
`patient_matching.scan_for_duplicates(db, patient)`) so a future PRP can
move it behind `event_publisher.publish("patient.created", ...)` and a
consumer without touching the endpoint.

### Merge workflow
`merge_patients(db, survivor_id, loser_id, reviewed_by) -> Patient`:
1. Load both patients `FOR UPDATE` (prevent concurrent merge race).
2. Reject if either is already merged (`merged_into_id IS NOT NULL`) —
   no merge chains; if A was already merged into B, you merge into B, not
   into the tombstoned A.
3. Reassign FKs from loser to survivor. **Currently this list has exactly
   one entry**: `Appointment.patient_id` (the only implemented module that
   references patients today). Write this as an explicit, documented,
   easily-extended list/registry — e.g. a module-level
   `PATIENT_FK_REASSIGNMENTS: list[tuple[type, str]] = [(Appointment, "patient_id")]`
   — with a comment telling future module authors to append here
   (`(Consultation, "patient_id")`, `(LabOrder, "patient_id")`, etc.) as
   each module lands. Do not build speculative reassignment logic for
   tables that don't have ORM models yet.
4. Set `loser.merged_into_id = survivor.id`.
5. Update the `patient_duplicate_candidates` row's `status = 'confirmed_merge'`,
   `reviewed_by`.
6. Audit-log the merge (`app.models.audit.record_audit_event`) — this is
   exactly the kind of action CLAUDE.md's HIPAA/GDPR framing means by
   "immutable audit logging": who merged what into what, when.
7. Commit once, all in one transaction.

`reject_duplicate_candidate(db, candidate_id, reviewed_by) -> None`: sets
`status = 'rejected'`, `reviewed_by`. No FK changes.

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| POST | /api/v1/patients | front_desk, nurse, doctor, system_admin | Create. Runs deterministic check first (409 on exact match unless `force=true`), then the probabilistic scan post-commit. |
| GET | /api/v1/patients/search | front_desk, nurse, doctor, system_admin | Query params: `name`, `dob`, `phone`. Search-before-create — reuses the same matching signals as the dedup engine, exposed for the UI's "does this patient already exist?" step. |
| GET | /api/v1/patients/{id} | front_desk (demographics only), nurse/doctor/system_admin (full record) | Response shape depends on role — see ABAC note below. |
| GET | /api/v1/patients/duplicates | front_desk, system_admin | Pending (`status='pending'`) candidate review queue. |
| POST | /api/v1/patients/duplicates/{id}/merge | front_desk, system_admin | Body: `{"survivor_id": uuid}` (must be one of the candidate pair). |
| POST | /api/v1/patients/duplicates/{id}/reject | front_desk, system_admin | Mark not-a-duplicate. |

**ABAC response shaping:** `GET /api/v1/patients/{id}` must call
`authorize(current_user, "patient", "read", patient)` (uses the existing
registered policies — `_front_desk_reads_demographics_only` for front_desk,
you'll need to register a new policy for doctor/nurse/system_admin full-read
if one doesn't already resolve via the `system_admin` wildcard + role checks)
and then return a **different Pydantic response model** depending on role:
`PatientDemographics` (no allergies, no national_id) for front_desk,
`PatientFullRecord` (everything except the raw encrypted bytes, which never
leave the DB layer — `EncryptedString` decrypts to plain fields on the ORM
object, do not add a "decrypt for API" step, the model attribute is already
plaintext once loaded) for clinical roles. This is the real enforcement of
"front_desk reads demographics only," not just a row-level gate.

---

## FILES TO CREATE / MODIFY

**Modify:**
- `database/schema.sql` — the additive changes above.
- `backend/app/models/patient.py` — add `user_id` column.
- `backend/app/routers/patients.py` — replace stub with the 6 endpoints.
- `backend/app/core/security.py` — register whatever additional `patient`
  policies are needed for doctor/nurse/system_admin full-read (check what's
  already covered by the `system_admin` wildcard before adding redundant ones).
- `backend/app/main.py` — no change expected (patients router is already included).

**Create:**
- `backend/app/services/patient_matching.py` — the matching engine described above.
- `backend/app/services/patient_service.py` — `create_patient`, `get_patient`,
  `search_patients`, `merge_patients`, `reject_duplicate_candidate`
  (business logic; routers stay thin, same split as `auth_service.py`).
- `backend/app/schemas/patient.py` — `PatientCreate`, `PatientDemographics`,
  `PatientFullRecord`, `DuplicateCandidateResponse`, `MergeRequest`.
- `frontend/src/services/patientService.ts`, `frontend/src/types/` additions
- `frontend/src/pages/PatientSearchPage.tsx` (search-before-create UX),
  `frontend/src/pages/PatientRegisterPage.tsx` (front-desk create form,
  shows the 409-conflict "possible existing patient" flow),
  `frontend/src/pages/DuplicateReviewPage.tsx` (the review queue + merge action)
- `backend/tests/test_patient_matching.py` (unit tests for the scoring
  function — pure logic, no DB needed for the weight math itself, though
  the trigram query does need one), `backend/tests/test_patients.py`
  (integration, same pattern as `test_auth.py`)

---

## PHASE EXECUTION PLAN

**Phase 1: Schema + matching engine (sequential — everything else depends on this)**
- BACKEND-AGENT: schema.sql additive changes, `models/patient.py` update,
  `services/patient_matching.py`

**Validation Gate 1:** matching engine unit-testable in isolation (score
function is pure Python); schema changes apply cleanly against a fresh
Postgres via `database/schema.sql`.

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: `schemas/patient.py`, `services/patient_service.py`,
  `routers/patients.py`, ABAC policy registration
- FRONTEND-AGENT: patient search/register/duplicate-review pages

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`,
`npm run lint`, `npm run type-check` (`tsc --noEmit`)

**Phase 3: Quality (parallel)**
- TEST-AGENT: `test_patient_matching.py` (scoring function edge cases:
  no phone, no national_id, exact everything, fuzzy-only, below-threshold
  no-candidate case), `test_patients.py` (create → 409 on exact dup, create
  → duplicate candidate appears in queue, search, merge reassigns
  appointment FKs correctly, merge rejects an already-merged patient,
  ABAC: front_desk response has no allergies field, doctor response does).
  Real Postgres via docker-compose (same pattern as the Auth module's
  TEST-AGENT — bring up postgres+redis, apply schema.sql, truncate between
  tests, tear down after).
- REVIEW-AGENT: OWASP + this module's specific risks — does the merge
  transaction actually hold row locks (race two concurrent merges of the
  same pair)? Does `force=true` bypass get audit-logged? Does the
  demographics-only response genuinely exclude every clinical field, or
  just the obvious one? Any place PHI (decrypted `full_name`/`phone`/etc.)
  ends up in a log line?

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply `database/schema.sql` to a fresh Postgres; import `patient_matching` and unit-test the scoring function with no DB |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `npm run type-check` |
| 3 | `pytest backend/tests/test_patient_matching.py backend/tests/test_patients.py --cov=app.services.patient_matching --cov=app.services.patient_service --cov=app.routers.patients --cov-fail-under=80` |

---

## NEXT STEP

Execute with parallel agents:
```
/execute-prp PRPs/patient-master-index-prp.md
```
