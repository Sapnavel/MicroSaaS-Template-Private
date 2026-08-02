# PRP: Lab Module (Sample Tracking Lifecycle)

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> and the Auth, Patient Master Index, and Clinical Consultation modules.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Lab (sample tracking lifecycle) |
| **Version** | 1.0 |
| **Created** | 2026-07-28 |
| **Complexity** | Medium (a forward-only state machine with per-transition data/role requirements is the concentrated "hard part" — smaller in scope than the scheduling engine or the prescription safety checker, but must not be treated as a trivial CRUD module) |

---

## MODULE OVERVIEW

**Description:** Tracks a lab test from order to result: a doctor orders a
test against a consultation, a sample physically gets collected, processed,
verified, and finally attached to the patient's record — each step
forward-only, each step audit-logged, with the actual result value (PHI)
only ever entering the system at the verify step.

**Why this module, why now:** It's the first module whose primary job is
*state-machine correctness* rather than scheduling, identity, or safety
scoring — a different flavor of "hard part" than the previous three
modules, and a template the (still-stubbed) Ward/OT and Billing modules can
follow for their own status progressions later.

**What already exists (do not recreate):**
- `database/schema.sql` §8 — `lab_sample_status` enum (`ordered, collected,
  processing, verified, attached`), `lab_orders` (consultation_id, patient_id,
  test_code, ordered_by, **status lives here**, created_at), `lab_samples`
  (lab_order_id, collected_by/collected_at, processed_at, verified_by/verified_at,
  `result_encrypted` PHI, attached_to_emr_at — **no status column of its
  own**, no `processed_by` column either).
- `backend/app/routers/lab.py` — stub with a TODO list; this PRP replaces it.
- `backend/app/core/security.py` — `authorize()`/`policy` registry pattern
  to extend.
- `backend/app/models/consultation.py`, `services/consultation_service.py`
  — `Consultation` (a lab order references one), the router-thin/service-thick
  pattern to copy.
- `backend/app/core/encryption.py` — `EncryptedString` for `result_encrypted`.
- `backend/app/models/audit.py` — `record_audit_event`, used on every transition.

**Key design decision this PRP makes (read before implementing):**
The schema's `lab_sample_status` enum column lives on **`lab_orders.status`**,
not on `lab_samples`. `lab_samples` has no status of its own — instead it
carries *timestamp+actor pairs* for the steps that have one
(`collected_by`/`collected_at`, `verified_by`/`verified_at`), plus two
timestamps with no actor column (`processed_at`, `attached_to_emr_at`).
Read this as: **the lifecycle status is a property of the order; the
sample row is where per-step evidence (who, when, and eventually the
result) accumulates.** The router stub's own TODO says
"`PATCH /api/v1/lab/samples/{id}/status`", which reads as if samples had a
status — this PRP corrects that to `PATCH /api/v1/lab/orders/{id}/transition`
(see ENDPOINTS) to match what the schema actually models. Say this
explicitly in code comments so a future reader isn't confused by the stub's
now-superseded wording.

**Result-capture design decision:** the schema gives exactly one
`result_encrypted` field and exactly two actor columns
(`collected_by`, `verified_by`) — there is no separate "who ran the test"
vs. "who verified it" distinction possible in this data model. This PRP
therefore defines the **verify transition as the point the result is
submitted** (`processing → verified` requires a `result` in the request
body; `verified_by`/`verified_at` record who submitted it). This
conflates "producing" and "verifying" the result into one step by
necessity of the schema, not by clinical ideal — document this limitation
plainly, do not silently pretend it's a two-person handoff the schema
doesn't support.

**MVP Scope:**
- [ ] ORM models for `LabOrder`, `LabSample`
- [ ] A forward-only transition state machine: `ordered → collected →
  processing → verified → attached`, no skipping, no going backward, every
  transition audit-logged
- [ ] Per-transition role requirements (see ENDPOINTS)
- [ ] `result` (PHI) only enters the system at the `verified` transition
- [ ] `attached` transition is the "now visible as part of the official
  record" step — requires `verified` already happened

---

## TECH STACK

| Layer | Technology |
|-------|------------|
| Backend | FastAPI + SQLAlchemy (no new infra) |
| Frontend | React + TypeScript |

---

## DATA MODEL (ORM models to add — schema.sql already has both tables)

- `LabOrder`: `consultation_id` (FK), `patient_id` (FK), `test_code`
  (String), `ordered_by` (FK users.id), `status` (the `lab_sample_status`
  Postgres enum — mirror as a Python `str, Enum` the same way
  `AppointmentStatus`/`TokenStatus` do, since this one genuinely IS a
  Postgres `CREATE TYPE ENUM`, unlike `PatientAllergy.severity`/
  `Prescription.status` which are plain TEXT — check schema.sql's actual
  `CREATE TYPE lab_sample_status AS ENUM (...)` to confirm before choosing
  plain `String` vs `Enum`), `created_at`.
- `LabSample`: `lab_order_id` (FK, effectively 1:1 with a `LabOrder` once
  collection happens — no `UNIQUE` constraint in schema.sql on this FK, so
  don't assume the DB enforces 1:1, enforce "at most one sample per order"
  at the service layer if you rely on that invariant), `collected_by`/`collected_at`
  (nullable — `NULL` until the `collected` transition), `processed_at`
  (nullable), `verified_by`/`verified_at` (nullable), `result_encrypted`
  → `EncryptedString`-mapped `result` field (PHI, nullable until verify),
  `attached_to_emr_at` (nullable).

---

## STATE MACHINE DESIGN (read before implementing — this is the core logic)

`backend/app/services/lab_workflow.py`, same rigor/shape discipline as
`services/prescription_safety.py` and `services/patient_matching.py`:

```
ordered --(collect)--> collected --(process)--> processing --(verify)--> verified --(attach)--> attached
```

- A module-level transition table (dict or small explicit function, your
  call — matching how `patient_matching.py`/`prescription_safety.py` keep
  their classification tables as diffable constants) mapping
  `current_status -> {allowed_next_status}`. Attempting any transition not
  in that table (skipping a step, going backward, transitioning an already-
  `attached` order) is a 409, not a 400 — this is a state conflict, not a
  malformed request.
- Per-transition requirements (enforce all of these, in one function per
  transition or one parameterized function — your call, document it):
  - `ordered → collected`: creates the `LabSample` row (one order should
    only ever get one `collected` transition — reject a second attempt to
    collect an already-collected order's sample, 409). Sets
    `collected_by`/`collected_at`.
  - `collected → processing`: sets `processed_at`. No actor column exists
    for this step in the schema — still audit-log the actor via
    `record_audit_event`, just don't invent a database column that doesn't
    exist.
  - `processing → verified`: **requires** a non-empty `result` in the
    request. Sets `verified_by`/`verified_at`/`result` (encrypted).
    Rejecting an empty/missing result here (422) is the one hard data
    requirement in this state machine — everything else is just a status flip.
  - `verified → attached`: sets `attached_to_emr_at`. No new data required
    — this step means "this already-verified result is now part of the
    official record," not "produce new information."
- Every successful transition calls `record_audit_event` (action e.g.
  `f"lab_order.transitioned_to_{new_status}"`, resource_type="lab_order",
  metadata recording old/new status and the actor) — this is exactly the
  kind of clinical state change CLAUDE.md's HIPAA/GDPR framing means by
  "immutable audit logging."
- PHI handling: `result` must never appear in a `logger.*` call anywhere in
  this module (same rule as `notes`/`symptoms`/allergy `reaction` in the
  Clinical Consultation module) — only in the DB column, the API response
  to an authorized reader, or (encrypted, per-field) the audit log's
  metadata if you choose to include it there (your call — including the
  actual result value in the audit log's JSONB metadata means it's stored
  in TWO places; consider recording only that a result was submitted, not
  its value, in the audit metadata, and say why in a comment either way).

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| POST | /api/v1/lab/orders | doctor (own consultation only — reuse the ownership pattern from Consultation's `authorize()` policies, adapted for `lab_order`), system_admin | `{consultation_id, test_code}`. `patient_id`/`ordered_by` derived server-side (from the consultation, and from `current_user.id` respectively) — never from client input. |
| GET | /api/v1/lab/orders/{id} | doctor (own via consultation ownership), nurse, lab_tech, system_admin | Full detail including the linked sample (if any) and, when present, the `result` — gate `result` visibility the same way the Clinical Consultation module gates PHI (consider whether every one of these roles should see the decrypted `result`, or whether front-line collection staff (nurse) should get a demographics-style reduced view without the result; make a call and document it, same discipline as the Patient module's front_desk/allergies_note decision). |
| GET | /api/v1/lab/orders | doctor (own), nurse, lab_tech, system_admin | Query params: `patient_id`, `consultation_id`, `status`. At least one required (same "no unbounded scan" rule as the Patient module's search endpoint). |
| PATCH | /api/v1/lab/orders/{id}/transition | see per-transition roles below | `{"to_status": "collected"\|"processing"\|"verified"\|"attached", "result": str \| null}` (`result` only meaningful/required for the `verified` transition; reject it being present for any other transition — don't let a client sneak a result value in on an unrelated transition). |

**Per-transition roles** (enforced inside the service function handling the
transition, not just at the router's `require_role` level, since one
endpoint handles four different transitions with different role
requirements):
- `ordered → collected`: nurse, lab_tech, system_admin (whoever physically
  draws the sample — commonly a nurse or phlebotomist, not necessarily a
  dedicated lab tech).
- `collected → processing`: lab_tech, system_admin.
- `processing → verified`: lab_tech, system_admin.
- `verified → attached`: lab_tech, system_admin.
- `doctor` never transitions anything — they order and read results, that's it.

---

## FILES TO CREATE / MODIFY

**Create:**
- `backend/app/models/lab.py` — `LabOrder`, `LabSample`, `LabOrderStatus` (if using a Python enum).
- `backend/app/services/lab_workflow.py` — the state machine above.
- `backend/app/services/lab_service.py` — `create_order`, `get_order`,
  `list_orders`, `transition_order` (calls into `lab_workflow.py`'s
  validation, does the actual DB writes + audit log).
- `backend/app/schemas/lab.py` — request/response schemas.
- `frontend/src/services/labService.ts`, `frontend/src/pages/LabOrderPage.tsx`
  (create an order from a consultation — likely linked from
  `ConsultationPage.tsx`, your call on whether to add a link there),
  `frontend/src/pages/LabWorklistPage.tsx` (a lab_tech/nurse-facing list of
  orders by status, with transition actions).
- `backend/tests/test_lab_workflow.py`, `backend/tests/test_lab.py`

**Modify:**
- `backend/app/models/__init__.py` — register new models.
- `backend/app/routers/lab.py` — replace the stub.
- `backend/app/core/security.py` — register whatever `lab_order` ABAC
  policies are needed for doctor ownership (mirroring the Consultation
  module's `doctor_id`-based ownership pattern — a lab order's "owner" is
  its consultation's doctor).
- `backend/app/main.py` — no change expected (router already included).

---

## PHASE EXECUTION PLAN

**Phase 1: Models + state machine (sequential)**
- BACKEND-AGENT: `models/lab.py`, `services/lab_workflow.py`.

**Validation Gate 1:** models import cleanly against a real Postgres
(apply `database/schema.sql` — check the ORM's `status` column type
(Enum vs String) actually matches the live `lab_sample_status` Postgres
type, the same class of check that's caught a real ORM/DDL mismatch twice
already in this codebase); the transition table itself is unit-testable
with no DB (given a current status and a requested status, is the
transition allowed — pure logic).

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: `schemas/lab.py`, `services/lab_service.py`,
  `routers/lab.py`, ABAC policies.
- FRONTEND-AGENT: order-creation UI, worklist UI.

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`.

**Phase 3: Quality (parallel)**
- TEST-AGENT: every legal transition succeeds with correct side effects
  (right columns stamped, right audit action); every illegal transition
  (skip a step, go backward, repeat a step, transition a nonexistent order)
  is rejected with the right status code; `verified` transition requires a
  non-empty `result` and rejects one being present on any other transition;
  role gating per transition (a nurse attempting `collected→processing`
  should be denied, a lab_tech attempting the same should succeed);
  doctor-ownership ABAC on read/create (a different doctor's lab order is
  not readable/creatable).
- REVIEW-AGENT: same bar as the last three modules' reviews — is there any
  path to a persisted `LabSample`/`LabOrder` status change that bypasses
  the state machine's validation (grep every place these models are
  mutated, not just the obvious endpoint)? Is `result` actually withheld
  from any role that shouldn't see it (whichever way ENDPOINTS' open
  question above got decided)? Is the audit log's `result`-inclusion
  decision (full value vs. presence-only) actually followed consistently?
  PHI in logs.

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply schema.sql to fresh Postgres; import new models; unit-test the transition table with no DB |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit` |
| 3 | `pytest backend/tests/test_lab_workflow.py backend/tests/test_lab.py --cov=app.services.lab_workflow --cov=app.services.lab_service --cov=app.routers.lab --cov-fail-under=80` |

---

## NEXT STEP

```
/execute-prp PRPs/lab-module-prp.md
```
