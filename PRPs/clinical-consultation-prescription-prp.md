# PRP: Clinical Consultation & Smart Prescription Engine

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> the Auth/RBAC module, and the Patient Master Index module.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Clinical Consultation & Smart Prescription Engine |
| **Version** | 1.0 |
| **Created** | 2026-07-28 |
| **Complexity** | Medium-High (safety-critical: a missed drug interaction or allergy conflict is a patient-harm bug, not just a data bug) |

---

## MODULE OVERVIEW

**Description:** Records what happens during a consultation — symptoms,
structured ICD-10/11 diagnoses, clinical notes — and lets a doctor write
prescriptions through a safety gate that checks the new drugs against the
patient's recorded allergies and their other currently-active prescriptions
before anything is finalized.

**Why this module, why now:** It's the first module in the build order that
writes patient-affecting clinical decisions (as opposed to identity/schedule
data), and it's the next dependency for Lab and Pharmacy. The safety-checker
is this module's version of the scheduling engine's tri-resource lock or the
patient module's matching engine — the concentrated hard problem worth
extra rigor.

**What already exists (do not recreate):**
- `database/schema.sql` §7 — `consultations`, `diagnoses`, `patient_allergies`,
  `drugs`, `drug_interactions`, `prescriptions`, `prescription_items`. All
  tables exist; **none have ORM models yet** (same situation the Patient
  module found `patient_duplicate_candidates` in).
- `backend/app/core/security.py` — `authorize()`, and an existing example
  policy `@policy("doctor", "consultation", "read")` /
  `_doctor_reads_own_consultations` (`consultation.doctor_id == user.id`) —
  already correct, reuse it; you'll need to add the write-side equivalent.
- `backend/app/core/encryption.py` — `EncryptedString` for
  `consultations.notes_encrypted` (PHI: free-text clinical notes).
- `backend/app/models/patient.py`, `services/patient_service.py` — the
  Patient module this one reads from (`patient_id` FKs everywhere).
- `backend/app/models/appointment.py` — `Appointment.status` (must be
  `in_progress` to start a consultation, per the existing stub's own TODO).
- `backend/app/routers/consultations.py` — stub with a TODO list; this PRP
  replaces it.
- The Patient module's `force=true` → 409 → explicit-retry → audit-log
  pattern (`services/patient_service.create_patient`) — this PRP reuses the
  same shape for prescription safety overrides, described below. Read that
  function before implementing the analogous one here.

**Key design decision this PRP makes (read before implementing):**
Like `Patient`, `Consultation` has **no `branch_id`** column in the schema.
Tenant scoping for a consultation is implicit through `doctor_id` (a doctor
belongs to exactly one branch) — `authorize()`'s tenant guard is a no-op
here too, by the same reasoning as the Patient module, and the *real*
control is `_doctor_reads_own_consultations`-style ownership policies, not
row-level branch isolation. Say this explicitly in code comments.

**MVP Scope:**
- [ ] ORM models for all 7 tables in schema.sql §7
- [ ] Start a consultation from an `in_progress` appointment; complete it
- [ ] Record structured ICD-10/11 diagnoses (one `is_primary` max per consultation)
- [ ] Record patient allergies (used by the safety checker — nothing writes
  to `patient_allergies` today)
- [ ] **Drug interaction + allergy safety checker**: three-tier outcome
  (BLOCK / OVERRIDE_REQUIRED / INFO), reusing the Patient module's
  409-then-explicit-override-then-audit-log shape
- [ ] A small seed dataset of drugs + known interactions (this is a
  scaffold, not a real drug database — see "Seed data" below)

---

## TECH STACK

| Layer | Technology |
|-------|------------|
| Backend | FastAPI + SQLAlchemy (no new infra — reuses Postgres, existing auth/ABAC) |
| Frontend | React + TypeScript |

---

## DATA MODEL (ORM models to add — schema.sql already has all of this, add nothing new to schema.sql itself unless you find a genuine gap the way the Patient module found one)

- `Consultation`: `appointment_id` (unique FK), `doctor_id`, `patient_id`,
  `symptoms` (plain text — not PHI-encrypted in the schema; it's not a
  precise diagnosis, treat it as already-acceptable per the existing DDL,
  don't add encryption that isn't in schema.sql), `notes` (maps to
  `notes_encrypted` via `EncryptedString`, same pattern as `Patient.phone`),
  `started_at`, `ended_at`.
- `Diagnosis`: `consultation_id`, `icd_code`, `description`, `is_primary`.
- `PatientAllergy`: `patient_id`, `substance`, `severity` (mild/moderate/severe),
  `reaction`.
- `Drug`: `name`, `generic_name`, `interaction_class`.
- `DrugInteraction`: composite PK (`drug_a_id`, `drug_b_id`), `severity`
  (contraindicated/major/moderate/minor), `description`. **No ordering
  guarantee between a/b** — any lookup must check both `(X,Y)` and `(Y,X)`,
  same problem the Patient module solved with `_ordered_pair` for
  `patient_duplicate_candidates`, except here you don't control insert
  order for reference data, so query both directions instead of enforcing
  canonical order at write time.
- `Prescription`: `consultation_id`, `patient_id`, `status` (draft/finalized/dispensed), `created_at`.
- `PrescriptionItem`: `prescription_id`, `drug_id`, `dosage`, `frequency`, `duration_days`.

---

## SAFETY ENGINE DESIGN (read before implementing — this is the core logic)

`backend/app/services/prescription_safety.py`, mirroring the rigor of
`services/patient_matching.py` (pure, DB-query-then-score-then-classify shape):

### Allergy matching
`patient_allergies.substance` is free text (a clinician typed "penicillin"
or "NSAIDs" at some point) — there is no allergen ontology in this schema
and building one is out of scope. Match a candidate drug against a
patient's allergies if, case-insensitively:
- `allergy.substance == drug.name`, OR
- `allergy.substance == drug.generic_name`, OR
- `allergy.substance == drug.interaction_class`

This is a real, documented limitation (a patient recorded as allergic to
"beta-lactams" won't match a drug named "Amoxicillin" unless
`interaction_class` happens to be exactly `"beta-lactams"` too) — say so in
the module docstring the same way the Patient module's PRP documented the
national_id-normalization limitation. Do not try to fuzzy-match this one;
an approximate allergy match that's wrong in either direction (false clear
or false alarm) is worse than an exact match with a documented gap.

### Interaction checking
"Concurrent active prescriptions" = other `Prescription` rows for this
patient with `status IN ('finalized', 'dispensed')` where at least one
`PrescriptionItem` is still within its course: `duration_days IS NULL`
(ongoing/chronic) OR `created_at + duration_days days >= today`. Gather the
`drug_id`s from those, plus the newly-requested drug_ids, and check every
new×existing pair (not new×new — two drugs in the *same* incoming
prescription should also be checked against each other, don't skip that)
against `drug_interactions` in both `(a,b)`/`(b,a)` orderings.

### Three-tier classification (not the PRP-stub's original binary framing —
made explicit here because "block vs. warn" undersells the allergy side,
which needs its own severity mapping)

| Source | Severity | Outcome |
|---|---|---|
| drug_interactions | contraindicated | **BLOCK** — cannot finalize, no override, remove the drug |
| patient_allergies | severe | **BLOCK** |
| drug_interactions | major, moderate | **OVERRIDE_REQUIRED** — 409 unless caller passes `override=true` + non-empty `override_reason` |
| patient_allergies | moderate | **OVERRIDE_REQUIRED** |
| drug_interactions | minor | **INFO** — proceeds automatically, surfaced in the response for the prescriber's awareness |
| patient_allergies | mild | **INFO** |

Compute `evaluate_prescription_safety(db, patient_id, drug_ids) ->
PrescriptionSafetyReport` returning all three tiers' findings (not just the
worst one) — the endpoint needs the full picture to decide BLOCK vs.
OVERRIDE_REQUIRED vs. proceed, and the UI needs the INFO items even on a
clean success.

### Endpoint flow (mirrors `patient_service.create_patient`'s force pattern)

`POST /api/v1/consultations/{id}/prescriptions`:
1. Run the safety check on the requested `drug_id`s.
2. Any BLOCK finding → reject outright (422), list the blocking findings,
   nothing persisted, no override possible at any input.
3. Any OVERRIDE_REQUIRED finding and `override != true` → 409 (same body
   shape discipline as the Patient module's 409: top-level fields, not
   buried in a generic `detail` wrapper) listing the findings that need
   acknowledgement.
4. `override == true` on a request that actually has OVERRIDE_REQUIRED
   findings → require `override_reason` (non-empty) → audit-log
   (`action="prescription.override_safety_warning"`, metadata = the
   findings overridden + the reason) → create `Prescription`
   (`status='finalized'`) + `PrescriptionItem`s.
5. No BLOCK/OVERRIDE_REQUIRED findings at all → create directly, still
   return the INFO findings (if any) in the response, no audit log needed
   (nothing was overridden).

---

## SEED DATA (scope limitation — say this plainly, don't fake completeness)

This is a scaffold, not a production drug/interaction database. Seed
roughly 10-15 common drugs and a handful of well-known interaction pairs
(e.g. Warfarin×Aspirin = major, Warfarin×NSAID-class = major) and 2-3
allergy-class examples, enough to make the safety checker demonstrably
testable end-to-end. Document in the seed script/migration that a real
deployment must integrate a licensed drug/interaction reference (e.g.
RxNorm, First Databank, DrugBank) — do not present the seed set as
clinically complete anywhere in code comments or docs.

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| POST | /api/v1/consultations | doctor, system_admin | Start from an `in_progress` appointment. 409 if that appointment already has a consultation (unique constraint) or isn't `in_progress`. |
| GET | /api/v1/consultations/{id} | doctor (own only, via existing policy), nurse, system_admin | Full detail: symptoms, notes, diagnoses, prescriptions. |
| PATCH | /api/v1/consultations/{id}/complete | doctor (own), system_admin | Sets `ended_at`. |
| POST | /api/v1/consultations/{id}/diagnoses | doctor (own), system_admin | `{icd_code, description, is_primary}`. Reject a second `is_primary=true` for the same consultation (400) — demote-the-old-one-automatically is NOT the behavior; make the client fix its request. |
| GET | /api/v1/patients/{patient_id}/allergies | doctor, nurse, system_admin | List. |
| POST | /api/v1/patients/{patient_id}/allergies | doctor, nurse, system_admin | Record one. Nurses commonly take allergy history at intake — this is the one clinical-write nurses get in this module. |
| POST | /api/v1/consultations/{id}/prescriptions | doctor (own), system_admin | The safety-gated flow above. |
| GET | /api/v1/consultations/{id}/prescriptions | doctor (own), nurse, system_admin | List, each with its items. |

ICD code validation: loose format check only (non-empty, roughly
`LETTER + 2 digits + optional .subcode`), not a lookup against a real
ICD-10/11 code table — that table doesn't exist in this schema and building
one is out of scope. Say so in the schema/validator's docstring.

---

## FILES TO CREATE / MODIFY

**Create:**
- `backend/app/models/consultation.py` — `Consultation`, `Diagnosis`, `PatientAllergy`, `Drug`, `DrugInteraction`, `Prescription`, `PrescriptionItem`.
- `backend/app/services/prescription_safety.py` — the engine above.
- `backend/app/services/consultation_service.py` — `start_consultation`,
  `complete_consultation`, `add_diagnosis`, `list_allergies`, `add_allergy`,
  `create_prescription` (calls the safety engine), `get_consultation`.
- `backend/app/schemas/consultation.py` — request/response schemas.
- A seed script/migration for the small drug/interaction dataset (your call
  on mechanism — a plain SQL seed file under `database/`, or a Python
  function callable from a fixture/startup hook; document which you chose
  and why).
- `frontend/src/services/consultationService.ts`,
  `frontend/src/pages/ConsultationPage.tsx` (symptoms/notes/diagnoses entry),
  `frontend/src/pages/PrescriptionPage.tsx` (drug entry + safety-check
  results + override flow), plus whatever allergy-entry UI fits naturally
  (a section within `ConsultationPage.tsx` is fine, doesn't need its own page).
- `backend/tests/test_prescription_safety.py`, `backend/tests/test_consultations.py`

**Modify:**
- `backend/app/models/__init__.py` — register the new models.
- `backend/app/routers/consultations.py` — replace the stub.
- `backend/app/core/security.py` — register the write-side `consultation`
  policy (doctor can write only their own) and whatever `patient_allergy`
  read/write policies are needed for nurse.
- `backend/app/main.py` — no change expected (router already included).

---

## PHASE EXECUTION PLAN

**Phase 1: Data model + safety engine (sequential)**
- BACKEND-AGENT: all 7 ORM models, `services/prescription_safety.py`, seed data.

**Validation Gate 1:** models import cleanly against a real Postgres
(apply `database/schema.sql`, confirm ORM ↔ DDL column agreement — the
Auth module's TEST-AGENT caught a real ORM/DDL mismatch this exact way
once already, don't skip this check); safety-engine pure classification
logic unit-testable without a DB for the tier-mapping table itself (the
allergy/interaction *lookup* queries need Postgres, the BLOCK/OVERRIDE/INFO
classification given a severity string does not).

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: schemas, `consultation_service.py`, router, ABAC policies.
- FRONTEND-AGENT: consultation/prescription pages.

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`.

**Phase 3: Quality (parallel)**
- TEST-AGENT: safety-engine classification table (every severity → correct
  tier), allergy match on all three matched fields (name/generic_name/
  interaction_class) and the documented non-match case, interaction check
  across both `(a,b)`/`(b,a)` orderings, BLOCK cannot be overridden at all,
  OVERRIDE_REQUIRED needs both `override=true` AND a non-empty reason,
  audit log written only on an actual override, `is_primary` diagnosis
  uniqueness, doctor-can't-read-another-doctor's-consultation (reuses the
  existing registered policy — confirm it's actually wired to the new
  endpoints, not just present in `core/security.py` unused).
- REVIEW-AGENT: same class of scrutiny as the last two modules — look
  specifically at whether the safety engine can be bypassed (e.g. can a
  prescription be created through some path that skips
  `evaluate_prescription_safety` entirely — check every code path that
  creates a `Prescription`/`PrescriptionItem`, not just the main endpoint),
  whether `override_reason` is actually required (not just accepted-if-present),
  PHI in logs (`notes`, `symptoms`, allergy `reaction` text), and whether
  `patient_allergies` write access is scoped correctly (should a doctor be
  able to add an allergy for a patient they've never consulted? Decide and
  justify, same style of call as the Patient module's front_desk/allergies_note finding).

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply schema.sql to fresh Postgres; import all new models; unit-test the tier-classification function with no DB |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit` |
| 3 | `pytest backend/tests/test_prescription_safety.py backend/tests/test_consultations.py --cov=app.services.prescription_safety --cov=app.services.consultation_service --cov=app.routers.consultations --cov-fail-under=80` |

---

## NEXT STEP

```
/execute-prp PRPs/clinical-consultation-prescription-prp.md
```
